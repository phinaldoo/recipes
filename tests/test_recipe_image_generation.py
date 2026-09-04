from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from app.ai import image_client
from app.ai.image_client import AIImageError, GeneratedRecipeImage
from app.config import Settings
from app.models import (
    ImageGenerationJob,
    Ingredient,
    IngredientGroup,
    InstructionStep,
    MediaAsset,
    Recipe,
    RecipeImage,
    User,
)
from app.services import image_generation, media
from app.services.image_generation import (
    build_recipe_image_edit_prompt,
    build_recipe_image_prompt,
    image_generation_available,
)
from app.services.media import RecipeCoverChanged, RecipeImageAlreadyExists
from app.services.storage import StoredFile


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "ai_api_key": "test-api-key",
        "ai_base_url": "https://images.example/v1",
        "ai_image_model": "gpt-image-1",
        "ai_image_quality": "high",
        "ai_max_retries": 0,
        "ai_timeout_seconds": 10,
        "ai_image_generation_enabled": True,
    }
    values.update(overrides)
    return Settings.model_validate(values)


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email="bild@example.test",
        display_name="Bildtest",
        password_hash="not-used",
        role="member",
        is_active=True,
    )


def _recipe(*, images: list[RecipeImage] | None = None) -> Recipe:
    group = IngredientGroup(
        id=uuid.uuid4(),
        title="Suppe",
        position=0,
        ingredients=[
            Ingredient(id=uuid.uuid4(), name="Kartoffeln", position=0),
            Ingredient(id=uuid.uuid4(), name="Lauch", position=1),
        ],
    )
    return Recipe(
        id=uuid.uuid4(),
        title="Cremige Kartoffelsuppe",
        slug="cremige-kartoffelsuppe",
        description="Sämig, hell und mit feinen Lauchringen.",
        notes="DIESER PRIVATE HINWEIS DARF NICHT IN DEN PROMPT",
        base_servings=4,
        serving_label="Personen",
        status="active",
        search_document="",
        ingredient_groups=[group],
        instruction_steps=[
            InstructionStep(
                id=uuid.uuid4(), position=0, text="Kartoffeln weich kochen und pürieren."
            )
        ],
        images=images or [],
    )


def _asset(*, key: str, kind: str = "generated_image") -> MediaAsset:
    return MediaAsset(
        id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        kind=kind,
        storage_key=key,
        original_filename=Path(key).name,
        mime_type="image/png" if kind == "generated_image" else "image/jpeg",
        byte_size=100,
        sha256="a" * 64,
        width=24,
        height=18,
    )


class _DB:
    def __init__(self, *, fail_flush: bool = False) -> None:
        self.added: list[object] = []
        self.fail_flush = fail_flush

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        if self.fail_flush:
            raise RuntimeError("flush failed")


def test_text_to_image_request_uses_generation_endpoint_and_decodes_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected = b"generated png"

    def post(url: str, **kwargs: Any) -> _Response:
        captured.update({"url": url, **kwargs})
        return _Response(
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(expected).decode("ascii"),
                        "revised_prompt": "Editorial food photo",
                    }
                ]
            }
        )

    monkeypatch.setattr(image_client.httpx, "post", post)
    result = image_client.generate_recipe_image("A bowl of soup", settings=_settings())

    assert result.data == expected
    assert result.revised_prompt == "Editorial food photo"
    assert captured["url"] == "https://images.example/v1/images/generations"
    assert captured["json"] == {
        "model": "gpt-image-1",
        "prompt": "A bowl of soup",
        "quality": "high",
        "size": "1536x1024",
    }
    assert captured["headers"]["Authorization"] == "Bearer test-api-key"


def test_text_to_image_requires_explicit_configuration() -> None:
    assert image_generation_available(_settings())
    assert not image_generation_available(_settings(ai_image_generation_enabled=False))
    assert not image_generation_available(_settings(ai_api_key="  "))
    with pytest.raises(AIImageError, match="nicht konfiguriert"):
        image_client.generate_recipe_image(
            "A bowl of soup",
            settings=_settings(ai_image_generation_enabled=False),
        )


def test_image_edit_request_sends_current_cover_as_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected = b"edited png"
    reference = b"current cover bytes"

    def post(url: str, **kwargs: Any) -> _Response:
        captured.update({"url": url, **kwargs})
        return _Response({"data": [{"b64_json": base64.b64encode(expected).decode("ascii")}]})

    monkeypatch.setattr(image_client.httpx, "post", post)
    result = image_client.edit_recipe_image(
        "Correct this recipe photo",
        reference,
        "image/jpeg",
        settings=_settings(),
    )

    assert result.data == expected
    assert captured["url"] == "https://images.example/v1/images/edits"
    assert captured["data"] == {
        "model": "gpt-image-1",
        "prompt": "Correct this recipe photo",
        "quality": "high",
        "size": "1536x1024",
    }
    field_name, upload = captured["files"][0]
    assert field_name == "image[]"
    assert upload == ("current-recipe-image", reference, "image/jpeg")


def test_recipe_prompt_uses_visual_recipe_context_but_not_private_notes() -> None:
    prompt = build_recipe_image_prompt(_recipe())
    edit_prompt = build_recipe_image_edit_prompt(_recipe())

    assert "Cremige Kartoffelsuppe" in prompt
    assert "Kartoffeln, Lauch" in prompt
    assert "weich kochen und pürieren" in prompt
    assert "PRIVATE HINWEIS" not in prompt
    assert "Keine Schrift" in prompt
    assert len(prompt) < 8000
    assert "bereitgestellte Bild" in edit_prompt
    assert "Rezeptdaten haben Vorrang" in edit_prompt
    assert "PRIVATE HINWEIS" not in edit_prompt


def test_generated_image_is_attached_as_cover_with_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "generated.png"
    thumbnail_path = tmp_path / "thumbnail.jpg"
    source.write_bytes(b"generated")
    thumbnail_path.write_bytes(b"thumbnail")
    stored = StoredFile(
        storage_key="generated.png",
        original_filename="generated.png",
        mime_type="image/png",
        byte_size=9,
        sha256="a" * 64,
        width=24,
        height=18,
    )
    asset = _asset(key="generated.png")
    thumbnail = _asset(key="thumbnail.jpg", kind="image_thumbnail")
    monkeypatch.setattr(media, "store_bytes", lambda *_args, **_kwargs: stored)
    monkeypatch.setattr(media, "create_asset", lambda *_args: asset)
    monkeypatch.setattr(media, "create_thumbnail_asset", lambda *_args: thumbnail)
    monkeypatch.setattr(media, "enforce_recipe_quota", Mock())
    monkeypatch.setattr(
        media,
        "resolve_storage_key",
        lambda key: source if key == "generated.png" else thumbnail_path,
    )
    db = _DB()

    image = media.add_generated_recipe_image(
        db,  # type: ignore[arg-type]
        _recipe(),
        _user(),
        b"generated",
        filename="suppe.png",
        alt_text="  KI-generiertes Bild der Suppe  ",
        generation_metadata={"model": "gpt-image-1"},
    )

    assert image.is_cover and image.position == 0
    assert image.asset is asset and image.thumbnail_asset is thumbnail
    assert image.alt_text == "KI-generiertes Bild der Suppe"
    assert image.generation_metadata == {"model": "gpt-image-1"}
    assert db.added == [image]


def test_regenerated_image_becomes_cover_and_retains_previous_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_asset = _asset(key="previous.png")
    previous = RecipeImage(
        id=uuid.uuid4(),
        media_asset_id=previous_asset.id,
        asset=previous_asset,
        position=3,
        is_cover=True,
    )
    recipe = _recipe(images=[previous])
    source = tmp_path / "generated.png"
    thumbnail_path = tmp_path / "thumbnail.jpg"
    source.write_bytes(b"generated")
    thumbnail_path.write_bytes(b"thumbnail")
    stored = StoredFile("generated.png", "generated.png", "image/png", 9, "a" * 64)
    asset = _asset(key="generated.png")
    thumbnail = _asset(key="thumbnail.jpg", kind="image_thumbnail")
    monkeypatch.setattr(media, "store_bytes", lambda *_args, **_kwargs: stored)
    monkeypatch.setattr(media, "create_asset", lambda *_args: asset)
    monkeypatch.setattr(media, "create_thumbnail_asset", lambda *_args: thumbnail)
    monkeypatch.setattr(media, "enforce_recipe_quota", Mock())
    monkeypatch.setattr(
        media,
        "resolve_storage_key",
        lambda key: source if key == "generated.png" else thumbnail_path,
    )
    db = _DB()

    regenerated = media.add_generated_recipe_image(
        db,  # type: ignore[arg-type]
        recipe,
        _user(),
        b"generated",
        filename="suppe-neu.png",
        alt_text="Neue Suppe",
        generation_metadata={"generation_mode": "regenerate"},
        previous_cover_image_id=previous.id,
    )

    assert previous.is_cover is False
    assert regenerated.is_cover is True
    assert regenerated.position == 4
    assert previous in recipe.images
    assert db.added == [regenerated]


def test_generated_image_refuses_existing_image_and_cleans_files_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_asset = _asset(key="existing.png")
    existing = RecipeImage(
        id=uuid.uuid4(),
        media_asset_id=existing_asset.id,
        asset=existing_asset,
        position=0,
        is_cover=True,
    )
    store = Mock()
    monkeypatch.setattr(media, "store_bytes", store)
    with pytest.raises(RecipeImageAlreadyExists):
        media.add_generated_recipe_image(
            _DB(),  # type: ignore[arg-type]
            _recipe(images=[existing]),
            _user(),
            b"generated",
            filename="suppe.png",
            alt_text="Suppe",
            generation_metadata={},
        )
    store.assert_not_called()

    with pytest.raises(RecipeCoverChanged):
        media.add_generated_recipe_image(
            _DB(),  # type: ignore[arg-type]
            _recipe(images=[existing]),
            _user(),
            b"generated",
            filename="suppe.png",
            alt_text="Suppe",
            generation_metadata={},
            previous_cover_image_id=uuid.uuid4(),
        )
    store.assert_not_called()

    source = tmp_path / "generated.png"
    thumbnail_path = tmp_path / "thumbnail.jpg"
    source.write_bytes(b"generated")
    thumbnail_path.write_bytes(b"thumbnail")
    stored = StoredFile("generated.png", "generated.png", "image/png", 9, "a" * 64)
    monkeypatch.setattr(media, "store_bytes", lambda *_args, **_kwargs: stored)
    monkeypatch.setattr(media, "create_asset", lambda *_args: _asset(key="generated.png"))
    monkeypatch.setattr(
        media,
        "create_thumbnail_asset",
        lambda *_args: _asset(key="thumbnail.jpg", kind="image_thumbnail"),
    )
    monkeypatch.setattr(media, "enforce_recipe_quota", Mock())
    monkeypatch.setattr(
        media,
        "resolve_storage_key",
        lambda key: source if key == "generated.png" else thumbnail_path,
    )
    with pytest.raises(RuntimeError, match="flush failed"):
        media.add_generated_recipe_image(
            _DB(fail_flush=True),  # type: ignore[arg-type]
            _recipe(),
            _user(),
            b"generated",
            filename="suppe.png",
            alt_text="Suppe",
            generation_metadata={},
        )
    assert not source.exists() and not thumbnail_path.exists()


def test_worker_generates_attaches_and_completes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    recipe = _recipe()
    user = _user()
    job = ImageGenerationJob(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        requested_by_user_id=user.id,
        generation_mode="create",
        status="running",
        current_stage="Passendes Rezeptbild wird erstellt",
        attempt_count=1,
    )
    generated_asset = _asset(key="generated/result.png")
    generated_thumbnail = _asset(key="derivatives/result.jpg", kind="image_thumbnail")
    attached = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        asset=generated_asset,
        thumbnail_asset=generated_thumbnail,
        position=0,
        is_cover=True,
    )

    class WorkerDB:
        def __init__(self) -> None:
            self.commits = 0
            self.rollbacks = 0

        def __enter__(self) -> WorkerDB:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def scalar(self, _statement: object) -> ImageGenerationJob:
            return job

        def get(self, model: object, identifier: object) -> User | None:
            if model is User and identifier == user.id:
                return user
            return None

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    db = WorkerDB()
    generate = Mock(return_value=GeneratedRecipeImage(b"valid image", "Refined prompt"))
    attach = Mock(return_value=attached)
    monkeypatch.setattr(image_generation, "get_settings", lambda: settings)
    monkeypatch.setattr(image_generation, "SessionLocal", lambda: db)
    monkeypatch.setattr(image_generation, "_claim_job", lambda *_args: job)
    monkeypatch.setattr(
        image_generation, "_recipe_for_generation", lambda *_args, **_kwargs: recipe
    )
    monkeypatch.setattr(image_generation, "generate_recipe_image", generate)
    monkeypatch.setattr(image_generation, "add_generated_recipe_image", attach)
    monkeypatch.setattr(image_generation, "resolve_storage_key", lambda key: Path(key))

    image_generation._process_image_generation_job(job.id)

    generate.assert_called_once()
    prompt = generate.call_args.args[0]
    assert recipe.title in prompt and "Kartoffeln" in prompt
    attach.assert_called_once()
    assert attach.call_args.kwargs["filename"] == "cremige-kartoffelsuppe-ki-bild.png"
    assert attach.call_args.kwargs["generation_metadata"]["quality"] == "high"
    assert attach.call_args.kwargs["generation_metadata"]["revised_prompt"] == "Refined prompt"
    assert job.status == "completed"
    assert job.result_image_id == attached.id
    assert job.current_stage == "Rezeptbild wurde erstellt"
    assert job.lease_token is None and job.lease_expires_at is None
    assert db.commits == 1 and db.rollbacks == 1


def test_worker_regeneration_edits_current_cover_and_promotes_new_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    user = _user()
    reference_path = tmp_path / "current-cover.png"
    reference_path.write_bytes(b"current cover image")
    previous_asset = _asset(key="current-cover.png")
    previous = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=uuid.uuid4(),
        asset=previous_asset,
        position=0,
        is_cover=True,
    )
    recipe = _recipe(images=[previous])
    previous.recipe_id = recipe.id
    job = ImageGenerationJob(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        requested_by_user_id=user.id,
        previous_cover_image_id=previous.id,
        generation_mode="regenerate",
        status="running",
        current_stage="Passendes Rezeptbild wird erstellt",
        attempt_count=1,
    )
    generated_asset = _asset(key="generated/new.png")
    attached = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        asset=generated_asset,
        position=1,
        is_cover=True,
    )

    class WorkerDB:
        def __enter__(self) -> WorkerDB:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def scalar(self, _statement: object) -> ImageGenerationJob:
            return job

        def get(self, model: object, identifier: object) -> User | None:
            return user if model is User and identifier == user.id else None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    edit = Mock(return_value=GeneratedRecipeImage(b"edited image", "Refined edit"))
    generate = Mock()
    attach = Mock(return_value=attached)
    monkeypatch.setattr(image_generation, "get_settings", lambda: settings)
    monkeypatch.setattr(image_generation, "SessionLocal", WorkerDB)
    monkeypatch.setattr(image_generation, "_claim_job", lambda *_args: job)
    monkeypatch.setattr(
        image_generation, "_recipe_for_generation", lambda *_args, **_kwargs: recipe
    )
    monkeypatch.setattr(image_generation, "edit_recipe_image", edit)
    monkeypatch.setattr(image_generation, "generate_recipe_image", generate)
    monkeypatch.setattr(image_generation, "add_generated_recipe_image", attach)
    monkeypatch.setattr(
        image_generation,
        "resolve_storage_key",
        lambda key: reference_path if key == "current-cover.png" else Path(key),
    )

    image_generation._process_image_generation_job(job.id)

    generate.assert_not_called()
    edit.assert_called_once()
    assert edit.call_args.args[1:] == (b"current cover image", "image/png")
    assert "bereitgestellte Bild" in edit.call_args.args[0]
    attach.assert_called_once()
    assert attach.call_args.kwargs["previous_cover_image_id"] == previous.id
    assert attach.call_args.kwargs["generation_metadata"]["source"] == ("recipe_cover_regeneration")
    assert job.status == "completed"
    assert job.current_stage == "Neues Rezeptbild wurde erstellt"


def test_regeneration_target_detects_intervening_cover_change() -> None:
    original = RecipeImage(id=uuid.uuid4(), position=0, is_cover=True)
    replacement = RecipeImage(id=uuid.uuid4(), position=1, is_cover=True)
    job = ImageGenerationJob(
        recipe_id=uuid.uuid4(),
        previous_cover_image_id=original.id,
        generation_mode="regenerate",
        status="running",
        current_stage="Bild wird erstellt",
        attempt_count=1,
    )
    recipe = _recipe(images=[replacement])

    assert image_generation._generation_target_problem(job, recipe) == (
        "Das Titelbild wurde inzwischen geändert"
    )


def test_recipe_detail_wires_accessible_generation_controls() -> None:
    project_root = Path(__file__).resolve().parents[1]
    template = (project_root / "app/templates/recipes/detail.html").read_text(encoding="utf-8")
    script = (project_root / "app/static/js/recipe-image-generation.js").read_text(encoding="utf-8")

    assert "for category in recipe.expanded_categories" in template
    assert "{{ category.name }}" in template
    assert "{{ category.path }}" not in template
    assert "data-generate-recipe-image" in template
    assert "t('recipe.image.regenerate')" in template
    assert "data-generation-mode" in template
    assert "image_generation_available or image_generation_job" in template
    assert (
        template.index('class="action-menu__panel"')
        < template.index("data-generate-recipe-image")
        < template.index('class="recipe-gallery"')
    )
    assert "recipe-gallery__generation" not in template
    assert "data-generation-status" not in template
    assert "aktuelle Titelbild wird der KI als Referenz übergeben" not in template
    assert "asset('js/recipe-image-generation.js')" in template
    assert "/image-generation/${jobId}" in script
    assert '{ method: "POST" }' in script
    assert "location.reload()" in script
