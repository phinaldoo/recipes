from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from PIL import Image

from app.api import categories as categories_api
from app.api import comments as comments_api
from app.api import exports as exports_api
from app.api import imports as imports_api
from app.api import media as media_api
from app.api import productivity as productivity_api
from app.api import recipes as recipes_api
from app.api import settings as settings_api
from app.models import ImageGenerationJob
from app.schemas.recipe import (
    CategoryCreate,
    CategoryMerge,
    CategoryMove,
    CategoryUpdate,
    CommentInput,
    ImageMetadataInput,
    RecipeInput,
    RestoreConfirmation,
)
from app.services.storage import InvalidUpload


class FakeResult:
    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[str, int]]:
        return self.rows


class FakeDB:
    def __init__(
        self, *, scalar_values: list[object] | None = None, get_result: object = None
    ) -> None:
        self.scalar_values = list(scalar_values or [])
        self.get_result = get_result
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self.expunge_calls = 0
        self.close_calls = 0
        self.execute_rows: list[tuple[str, int]] = []

    def scalar(self, _statement: object) -> object | None:
        return self.scalar_values.pop(0) if self.scalar_values else None

    def get(self, _model: object, _identifier: object) -> object | None:
        return self.get_result

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        self.flushes += 1
        now = datetime.now(UTC)
        for item in self.added:
            if hasattr(item, "id") and getattr(item, "id", None) is None:
                item.id = uuid.uuid4()
            if hasattr(item, "created_at") and getattr(item, "created_at", None) is None:
                item.created_at = now
            if hasattr(item, "updated_at") and getattr(item, "updated_at", None) is None:
                item.updated_at = now

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def execute(self, _statement: object) -> FakeResult:
        return FakeResult(self.execute_rows)

    def expunge_all(self) -> None:
        self.expunge_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _user(*, role: str = "member") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        language="de",
        password_hash="stored-password-hash",
        visible_name="Testperson",
    )


def _recipe(*, deleted: bool = False, status: str = "active") -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        title="Kartoffelsuppe",
        slug="kartoffelsuppe",
        description="Cremig",
        base_servings=4,
        serving_label="Personen",
        total_time_minutes=35,
        nutrition=[
            SimpleNamespace(
                basis="per_serving",
                energy_kj=None,
                energy_kcal=347,
                fat_g=13,
                saturated_fat_g=None,
                carbohydrates_g=47,
                sugars_g=None,
                fiber_g=None,
                protein_g=10,
                salt_g=None,
                note=None,
            )
        ],
        status=status,
        deleted_at=now if deleted else None,
        categories=[SimpleNamespace(id=uuid.uuid4(), name="Suppen", path="Suppen")],
        comments=[SimpleNamespace(deleted_at=None), SimpleNamespace(deleted_at=now)],
        images=[],
        cover_image=None,
        created_at=now,
        updated_at=now,
    )


def _category() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        parent_id=None,
        name="Backen",
        path="Backen",
        position=0,
        origin="manual",
        recipe_links=[object()],
        children=[object()],
    )


def _comment(author_id: uuid.UUID | None = None) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        recipe_id=uuid.uuid4(),
        author_user_id=author_id or uuid.uuid4(),
        author_name_snapshot="Ada",
        text="Mehr Muskat",
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )


def test_recipe_api_success_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(scalar_values=[None])
    user = _user()
    recipe = _recipe()
    payload = RecipeInput(title="Kartoffelsuppe", base_servings="4")
    monkeypatch.setattr(recipes_api, "list_recipes", lambda *_args, **_kwargs: ([recipe], 1, 1, 1))
    monkeypatch.setattr(recipes_api, "create_recipe", lambda *_args: recipe)
    monkeypatch.setattr(recipes_api, "get_recipe", lambda *_args, **_kwargs: recipe)
    update = Mock()
    monkeypatch.setattr(recipes_api, "update_recipe", update)
    delete = Mock()
    monkeypatch.setattr(recipes_api, "soft_delete_recipe", delete)
    restore = Mock()
    monkeypatch.setattr(recipes_api, "restore_recipe", restore)

    listing = recipes_api.index(
        q="suppe", category_ids=[], sort="updated_desc", page=1, page_size=24, _=user, db=db
    )
    assert listing["pagination"] == {"page": 1, "page_size": 24, "total": 1, "pages": 1}
    assert listing["items"][0]["title"] == "Kartoffelsuppe"
    assert listing["items"][0]["nutrition"][0]["energy_kcal"] == "347"
    assert recipes_api.create(payload, user=user, db=db)["redirect"].endswith(str(recipe.id))
    assert recipes_api.detail(recipe.id, _=user, db=db)["recipe"]["slug"] == "kartoffelsuppe"

    result = recipes_api.update(recipe.id, payload, user=user, db=db)
    assert result["message"] == "Rezept gespeichert"
    update.assert_called_once()

    assert recipes_api.delete(recipe.id, user=user, db=db)["redirect"] == "/rezepte"
    delete.assert_called_once()
    recipe.deleted_at = datetime.now(UTC)
    assert recipes_api.restore(recipe.id, user=user, db=db)["message"].startswith("Das Rezept")
    restore.assert_called_once()
    assert db.commits == 4


def test_recipe_api_rolls_back_domain_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    user = _user()
    payload = RecipeInput(title="Test", base_servings="1")
    monkeypatch.setattr(
        recipes_api, "create_recipe", Mock(side_effect=ValueError("Kategorie doppelt"))
    )
    with pytest.raises(HTTPException) as create_error:
        recipes_api.create(payload, user=user, db=db)
    assert create_error.value.status_code == 422

    recipe = _recipe(status="active")
    monkeypatch.setattr(recipes_api, "get_recipe", lambda *_args, **_kwargs: recipe)
    monkeypatch.setattr(
        recipes_api,
        "update_recipe",
        Mock(side_effect=recipes_api.RecipeConflict("Zwischenzeitlich geändert")),
    )
    with pytest.raises(HTTPException) as update_error:
        recipes_api.update(recipe.id, payload, user=user, db=db)
    assert update_error.value.status_code == 409
    assert db.rollbacks == 2

    recipe.deleted_at = None
    with pytest.raises(HTTPException) as restore_error:
        recipes_api.restore(recipe.id, user=user, db=db)
    assert restore_error.value.status_code == 409


def test_version_list_is_paginated_without_snapshots_and_detail_is_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _recipe()
    user = _user()
    version = SimpleNamespace(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        version_number=42,
        change_summary="Rezept geändert",
        created_at=datetime.now(UTC),
        changed_by=SimpleNamespace(visible_name="Ada"),
        snapshot={"title": "Kartoffelsuppe", "notes": "Geheimnis"},
    )
    monkeypatch.setattr(
        productivity_api,
        "version_history",
        lambda *_args, **_kwargs: (
            [(version, [{"field": "Titel", "before": "Alt", "after": "Neu"}])],
            101,
            5,
            2,
        ),
    )

    listing = productivity_api.versions_index(recipe.id, page=2, page_size=25, _=user, db=FakeDB())
    assert listing["page"] == 2
    assert listing["pages"] == 5
    assert listing["total"] == 101
    assert "snapshot" not in listing["items"][0]

    monkeypatch.setattr(productivity_api, "get_recipe", lambda *_args, **_kwargs: recipe)
    detail = productivity_api.version_detail(
        recipe.id, version.id, _=user, db=FakeDB(get_result=version)
    )
    assert detail["snapshot"] == version.snapshot

    version.recipe_id = uuid.uuid4()
    with pytest.raises(HTTPException) as wrong_recipe:
        productivity_api.version_detail(
            recipe.id, version.id, _=user, db=FakeDB(get_result=version)
        )
    assert wrong_recipe.value.status_code == 404


def test_category_api_crud_and_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    category = _category()
    db = FakeDB(get_result=category)
    user = _user()
    monkeypatch.setattr(categories_api, "category_tree", lambda _db: [category])
    monkeypatch.setattr(categories_api, "create_category", lambda *_args: category)
    monkeypatch.setattr(categories_api, "update_category", lambda _db, item, _payload: item)
    monkeypatch.setattr(categories_api, "merge_category", lambda *_args: 3)
    monkeypatch.setattr(categories_api, "delete_category", lambda *_args: 2)

    assert categories_api.index(_=user, db=db)["items"][0]["recipe_count"] == 1
    assert categories_api.create(CategoryCreate(name="Backen"), _=user, db=db)["message"]
    assert (
        categories_api.update(category.id, CategoryUpdate(name="Kuchen"), _=user, db=db)[
            "category"
        ]["name"]
        == "Backen"
    )
    assert (
        categories_api.move(category.id, CategoryMove(parent_id=None, position=2), _=user, db=db)[
            "message"
        ]
        == "Kategorie verschoben"
    )
    assert (
        categories_api.merge(
            category.id, CategoryMerge(target_category_id=category.id), _=user, db=db
        )["moved_recipe_links"]
        == 3
    )
    assert categories_api.delete(category.id, _=user, db=db)["affected_recipes"] == 2
    assert db.commits == 5

    db.get_result = None
    with pytest.raises(HTTPException) as exc:
        categories_api._category(db, uuid.uuid4())  # type: ignore[arg-type]
    assert exc.value.status_code == 404


def test_comment_api_permissions_and_crud(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    comment = _comment(user.id)
    recipe = SimpleNamespace(id=comment.recipe_id, comments=[comment])
    db = FakeDB(scalar_values=[comment, comment])
    monkeypatch.setattr(comments_api, "get_recipe", lambda *_args, **_kwargs: recipe)
    monkeypatch.setattr(comments_api, "create_comment", lambda *_args: comment)
    update = Mock()
    delete = Mock()
    monkeypatch.setattr(comments_api, "update_comment", update)
    monkeypatch.setattr(comments_api, "delete_comment", delete)

    serialized = comments_api.serialize(comment, user)
    assert serialized["can_edit"] is True and serialized["can_delete"] is True
    assert (
        comments_api.list_comments(recipe.id, user=user, db=db)["items"][0]["text"] == "Mehr Muskat"
    )
    assert (
        comments_api.create(recipe.id, CommentInput(text="Mehr Muskat"), user=user, db=db)[
            "message"
        ]
        == "Notiz hinzugefügt"
    )
    assert (
        comments_api.update(
            recipe.id, comment.id, CommentInput(text="Weniger Salz"), user=user, db=db
        )["message"]
        == "Notiz gespeichert"
    )
    assert (
        comments_api.delete(recipe.id, comment.id, user=user, db=db)["message"] == "Notiz gelöscht"
    )
    update.assert_called_once()
    delete.assert_called_once()
    assert db.commits == 3

    with pytest.raises(HTTPException) as exc:
        comments_api._comment(FakeDB(), recipe.id, uuid.uuid4())  # type: ignore[arg-type]
    assert exc.value.status_code == 404


def test_export_api_json_and_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    recipe = _recipe()
    db = FakeDB()
    user = _user()
    monkeypatch.setattr(exports_api, "get_recipe", lambda *_args: recipe)
    monkeypatch.setattr(
        exports_api,
        "recipe_package_dict",
        lambda *_args, **_kwargs: {"schema_version": "1.1", "recipe": {"title": recipe.title}},
    )
    response = exports_api.export_json(recipe.id, include_originals=False, _=user, db=db)
    assert json.loads(response.body)["recipe"]["title"] == recipe.title
    assert response.headers["content-disposition"].endswith('kartoffelsuppe.rezept.json"')

    render = AsyncMock(return_value=b"%PDF-test")
    monkeypatch.setattr(exports_api, "render_recipe_pdf", render)
    pdf = asyncio.run(
        exports_api.export_pdf(recipe.id, servings=8, include_comments=True, _=user, db=db)
    )
    assert pdf.body == b"%PDF-test"
    assert db.expunge_calls == 1 and db.close_calls == 1
    render.assert_awaited_once_with(recipe, desired_servings=8, include_comments=True)


class AsyncUpload:
    def __init__(self, content: bytes, filename: str = "recipe.json") -> None:
        self.content = content
        self.filename = filename
        self.calls = 0

    async def read(self, _size: int = -1) -> bytes:
        self.calls += 1
        return self.content if self.calls == 1 else b""


def test_import_json_success_and_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    recipe = _recipe()
    db = FakeDB()
    payload = b'{"schema_version":"1.1","recipe":{"title":"Suppe","base_servings":"4"}}'
    monkeypatch.setattr(imports_api, "import_recipe_package", lambda *_args: recipe)
    result = asyncio.run(
        imports_api.import_json(AsyncUpload(payload), user=user, db=db)  # type: ignore[arg-type]
    )
    assert result["recipe_id"] == str(recipe.id)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            imports_api.import_json(AsyncUpload(b"not-json"), user=user, db=db)  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 422
    assert db.rollbacks == 1


def test_import_url_status_retry_cancel_and_access(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    db = FakeDB(scalar_values=[0])
    send = Mock(side_effect=RuntimeError("redis unavailable"))
    monkeypatch.setattr(imports_api.import_batch_task, "send", send)
    monkeypatch.setattr(imports_api, "validate_public_url", lambda url: url)
    payload = imports_api.URLImportPayload(urls=["https://example.com/recipe"])
    result = imports_api.import_urls(payload, user=user, db=db)
    assert result["redirect"].startswith("/importieren/")
    batch = next(item for item in db.added if item.__class__.__name__ == "ImportBatch")
    job = next(item for item in db.added if item.__class__.__name__ == "ImportJob")
    batch.jobs = [job]
    batch.completed_jobs = 0
    batch.failed_jobs = 0
    job.batch = batch
    job.attempt_count = 0
    job.error_code = None
    job.error_message = None
    job.result_recipe_id = None
    job.source_asset_id = None

    db.scalar_values = [batch]
    assert imports_api.batch_status(batch.id, user=user, db=db)["jobs"][0]["input_type"] == "url"
    db.get_result = job
    assert imports_api.job_status(job.id, user=user, db=db)["status"] == "queued"

    with pytest.raises(HTTPException) as exc:
        imports_api.retry_job(job.id, user=user, db=db)
    assert exc.value.status_code == 409
    job.status = "failed"
    recompute = Mock()
    monkeypatch.setattr(imports_api, "recompute_batch", recompute)
    monkeypatch.setattr(imports_api.import_job_task, "send", Mock())
    assert imports_api.retry_job(job.id, user=user, db=db)["message"].endswith("versucht.")
    recompute.assert_called()
    job.status = "queued"
    assert imports_api.cancel_job(job.id, user=user, db=db)["message"].startswith("Import")
    assert job.status == "cancelled" and job.finished_at is not None

    other = _user()
    with pytest.raises(HTTPException) as access_error:
        imports_api._check_batch_access(batch, other)  # type: ignore[arg-type]
    assert access_error.value.status_code == 403


def test_import_urls_rejects_unsafe_ordinal_before_creating_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    db = FakeDB(scalar_values=[0])
    dispatch = Mock()
    monkeypatch.setattr(imports_api.import_batch_task, "send", dispatch)

    def validate(url: str) -> str:
        if "unsafe" in url:
            raise imports_api.UnsafeURL("Interne Netzwerkziele sind nicht erlaubt")
        return url

    monkeypatch.setattr(imports_api, "validate_public_url", validate)
    payload = imports_api.URLImportPayload(
        urls=["https://example.com/rezept", "https://unsafe.example/rezept"]
    )

    with pytest.raises(HTTPException) as exc:
        imports_api.import_urls(payload, user=user, db=db)

    assert exc.value.status_code == 422
    assert exc.value.detail == "Webadresse 2: Interne Netzwerkziele sind nicht erlaubt"
    assert db.added == []
    assert db.flushes == 0
    assert db.commits == 0
    dispatch.assert_not_called()


def test_import_capacity_and_missing_jobs() -> None:
    user = _user()
    with pytest.raises(HTTPException) as capacity_error:
        imports_api._ensure_import_capacity(FakeDB(scalar_values=[50]), user, 1)  # type: ignore[arg-type]
    assert capacity_error.value.status_code == 429
    with pytest.raises(HTTPException) as batch_error:
        imports_api.batch_status(uuid.uuid4(), user=user, db=FakeDB())  # type: ignore[arg-type]
    assert batch_error.value.status_code == 404
    with pytest.raises(HTTPException) as job_error:
        imports_api.job_status(uuid.uuid4(), user=user, db=FakeDB())  # type: ignore[arg-type]
    assert job_error.value.status_code == 404


def test_import_files_success_validation_and_source_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user()
    db = FakeDB(scalar_values=[0])
    stored_path = tmp_path / "original.png"
    stored_path.write_bytes(b"png")
    stored = SimpleNamespace(storage_key="original.png", mime_type="image/png")
    asset = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(imports_api, "store_upload", AsyncMock(return_value=stored))
    monkeypatch.setattr(imports_api, "resolve_storage_key", lambda _key: stored_path)
    monkeypatch.setattr(imports_api, "create_asset", lambda *_args: asset)
    send = Mock()
    monkeypatch.setattr(imports_api.import_batch_task, "send", send)

    result = asyncio.run(
        imports_api.import_files(
            [AsyncUpload(b"png", "food.png")],
            user=user,
            db=db,  # type: ignore[list-item]
        )
    )
    assert result["redirect"].startswith("/importieren/")
    assert db.commits == 1
    send.assert_called_once()

    monkeypatch.setattr(
        imports_api, "store_upload", AsyncMock(side_effect=InvalidUpload("Nicht erlaubt"))
    )
    invalid_db = FakeDB(scalar_values=[0])
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(
            imports_api.import_files(
                [AsyncUpload(b"bad", "bad.exe")],  # type: ignore[list-item]
                user=user,
                db=invalid_db,
            )
        )
    assert invalid.value.status_code == 422 and invalid_db.rollbacks == 1

    with pytest.raises(HTTPException) as count_error:
        asyncio.run(imports_api.import_files([], user=user, db=FakeDB()))
    assert count_error.value.status_code == 422

    batch = SimpleNamespace(created_by_user_id=user.id)
    source_asset = SimpleNamespace(storage_key="original.png", mime_type="image/png")
    job = SimpleNamespace(batch=batch, source_asset=source_asset)
    response = imports_api.view_job_source(
        uuid.uuid4(),
        user=user,
        db=FakeDB(get_result=job),  # type: ignore[arg-type]
    )
    assert response.path == stored_path
    with pytest.raises(HTTPException) as missing:
        imports_api.view_job_source(uuid.uuid4(), user=user, db=FakeDB())  # type: ignore[arg-type]
    assert missing.value.status_code == 404


def test_candidate_image_endpoint_returns_only_the_assigned_source_crop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    source_path = tmp_path / "two-recipes.png"
    source = Image.new("RGB", (200, 100), "red")
    source.paste("blue", (100, 0, 200, 100))
    source.save(source_path, format="PNG")
    batch = SimpleNamespace(created_by_user_id=user.id)
    source_asset = SimpleNamespace(storage_key="source.png", mime_type="image/png")
    job = SimpleNamespace(batch=batch, source_asset=source_asset)
    candidate = SimpleNamespace(
        job=job,
        thumbnail_asset=None,
        image_asset=None,
        image_region_json={
            "page": 1,
            "bounding_box": {"left": 500, "top": 0, "right": 1000, "bottom": 1000},
            "description": "blaues Gericht",
            "confidence": 0.96,
        },
    )
    monkeypatch.setattr(imports_api, "resolve_storage_key", lambda _key: source_path)

    response = imports_api.view_candidate_image(
        uuid.uuid4(),
        user=user,
        db=FakeDB(get_result=candidate),  # type: ignore[arg-type]
    )

    assert response.media_type == "image/png"
    with Image.open(BytesIO(response.body)) as crop:
        assert crop.size == (100, 100)
        assert crop.getpixel((50, 50)) == (0, 0, 255)


def test_confirm_candidates_promotes_selection_and_removes_discarded_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    batch = SimpleNamespace(id=uuid.uuid4(), created_by_user_id=user.id)
    recipe = SimpleNamespace(id=uuid.uuid4())
    discarded_image = tmp_path / "discarded.png"
    discarded_thumbnail = tmp_path / "discarded-thumb.jpg"
    discarded_image.write_bytes(b"image")
    discarded_thumbnail.write_bytes(b"thumbnail")
    selected_id = uuid.uuid4()
    captured: dict[str, object] = {}

    def promote(_db: object, *, batch: object, selected_ids: set[uuid.UUID], user: object):
        captured.update(batch=batch, selected_ids=selected_ids, user=user)
        return [recipe], [discarded_image, discarded_thumbnail]

    monkeypatch.setattr(imports_api, "import_selected_candidates", promote)
    db = FakeDB(scalar_values=[batch])

    result = imports_api.confirm_candidates(
        batch.id,
        imports_api.ImportSelectionPayload(selected_candidate_ids=[selected_id]),
        user=user,
        db=db,  # type: ignore[arg-type]
    )

    assert result["recipe_ids"] == [str(recipe.id)]
    assert result["redirect"] == f"/rezepte/{recipe.id}"
    assert captured == {"batch": batch, "selected_ids": {selected_id}, "user": user}
    assert db.commits == 1
    assert not discarded_image.exists() and not discarded_thumbnail.exists()


def _backup_job(tmp_path: Path, *, operation: str = "export") -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        requested_by_user_id=uuid.uuid4(),
        operation=operation,
        status="completed" if operation == "export" else "preflight_complete",
        progress=100 if operation == "export" else 0,
        current_stage="Abgeschlossen",
        summary_json={"counts": {"recipes": 1}},
        error_message=None,
        created_at=now,
        finished_at=now,
        archive_filename="backup.zip",
        archive_sha256="a" * 64,
    )


def test_settings_summary_backup_job_and_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(role="admin")
    db = FakeDB(scalar_values=[2, 3, 4, 5, 6])
    db.execute_rows = [("recipe_image", 120), ("original_upload", 80)]
    summary = settings_api.system_summary(_=user, db=db)
    assert summary["counts"] == {
        "users": 2,
        "recipes": 3,
        "comments": 4,
        "categories": 5,
        "files": 6,
    }
    assert summary["storage_bytes_by_kind"]["recipe_image"] == 120

    db = FakeDB(scalar_values=[None])
    monkeypatch.setattr(settings_api.backup_task, "send", Mock(side_effect=RuntimeError("redis")))
    created = settings_api.create_backup(user=user, db=db)
    assert created["job"]["operation"] == "export"
    assert db.commits == 1
    with pytest.raises(HTTPException) as conflict:
        settings_api.create_backup(user=user, db=FakeDB(scalar_values=[uuid.uuid4()]))
    assert conflict.value.status_code == 409

    job = _backup_job(tmp_path)
    path = tmp_path / "backup.zip"
    path.write_bytes(b"zip")
    fake_settings = SimpleNamespace(
        backup_temp_root=tmp_path,
        backup_download_retention_hours=24,
        app_secret_key="test-secret-key-long-enough-for-hmac",
    )
    monkeypatch.setattr(settings_api, "get_settings", lambda: fake_settings)
    response = settings_api.backup_download(job.id, _=user, db=FakeDB(get_result=job))  # type: ignore[arg-type]
    assert response.path == path
    assert settings_api.backup_status(job.id, _=user, db=FakeDB(get_result=job))["job"][
        "download_available"
    ]

    job.finished_at = datetime.now(UTC) - timedelta(hours=25)
    with pytest.raises(HTTPException) as expired:
        settings_api.backup_download(job.id, _=user, db=FakeDB(get_result=job))  # type: ignore[arg-type]
    assert expired.value.status_code == 410
    assert not path.exists()


def test_settings_job_lookup_and_restore_status() -> None:
    user = _user(role="admin")
    job = _backup_job(Path("."), operation="restore")
    assert (
        settings_api.restore_status(job.id, _=user, db=FakeDB(get_result=job))["job"]["operation"]
        == "restore"
    )  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as exc:
        settings_api._job(FakeDB(), uuid.uuid4(), "restore")  # type: ignore[arg-type]
    assert exc.value.status_code == 404


def test_restore_preflight_and_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user(role="admin")
    fake_settings = SimpleNamespace(
        backup_temp_root=tmp_path,
        storage_root=tmp_path,
        max_backup_upload_bytes=10_000,
        app_secret_key="test-secret-key-long-enough-for-hmac",
    )
    monkeypatch.setattr(settings_api, "get_settings", lambda: fake_settings)
    preflight = SimpleNamespace(
        required_disk_bytes=1,
        model_dump=lambda **_kwargs: {
            "valid": True,
            "counts": {"users": 1, "recipes": 2},
            "required_disk_bytes": 1,
        },
    )
    monkeypatch.setattr(settings_api, "preflight_backup", lambda _path: preflight)
    monkeypatch.setattr(
        settings_api.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10_000, used=1, free=9_999),
    )
    db = FakeDB()
    result = asyncio.run(
        settings_api.restore_preflight(
            AsyncUpload(b"valid-zip", "backup.zip"),  # type: ignore[arg-type]
            user=user,
            db=db,
        )
    )
    token = result["preflight_token"]
    job = next(item for item in db.added if item.__class__.__name__ == "BackupRestoreJob")
    assert token.startswith(f"{job.id}.")
    assert (tmp_path / job.archive_filename).read_bytes() == b"valid-zip"

    job.requested_by_user_id = user.id
    job.status = "preflight_complete"
    job.operation = "restore"
    monkeypatch.setattr(settings_api, "verify_password", lambda *_args: True)
    send = Mock()
    monkeypatch.setattr(settings_api.restore_task, "send", send)
    start_db = FakeDB(scalar_values=[None, None], get_result=job)
    confirmation = RestoreConfirmation(
        preflight_token=token,
        confirmation="WIEDERHERSTELLEN",
        password="correct-password",
    )
    started = settings_api.start_restore(confirmation, user=user, db=start_db)
    assert started["job"]["status"] == "queued"
    assert start_db.commits == 1
    send.assert_called_once()

    with pytest.raises(HTTPException) as malformed:
        settings_api.start_restore(
            confirmation.model_copy(update={"preflight_token": "x" * 32}),
            user=user,
            db=FakeDB(),
        )
    assert malformed.value.status_code == 422


def test_recipe_image_generation_api_queues_reuses_and_scopes_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    recipe = _recipe()
    send = Mock()
    monkeypatch.setattr(media_api, "get_recipe", lambda *_args, **_kwargs: recipe)
    monkeypatch.setattr(media_api, "image_generation_available", lambda *_args: True)
    monkeypatch.setattr(media_api.image_generation_task, "send", send)
    monkeypatch.setattr(media_api, "get_active_image_generation_job", lambda *_args: None)

    db = FakeDB()
    result = media_api.start_image_generation(recipe.id, user=user, db=db)  # type: ignore[arg-type]
    job = db.added[0]

    assert isinstance(job, ImageGenerationJob)
    assert result["job"]["status"] == "queued"  # type: ignore[index]
    assert job.recipe_id == recipe.id and job.requested_by_user_id == user.id
    assert job.generation_mode == "create" and job.previous_cover_image_id is None
    assert db.commits == 1
    send.assert_called_once_with(str(job.id))

    monkeypatch.setattr(media_api, "get_active_image_generation_job", lambda *_args: job)
    reused_db = FakeDB()
    reused = media_api.start_image_generation(  # type: ignore[arg-type]
        recipe.id, user=user, db=reused_db
    )
    assert reused["job"]["id"] == str(job.id)  # type: ignore[index]
    assert reused_db.added == [] and reused_db.commits == 0
    send.assert_called_once()

    status = media_api.image_generation_status(
        recipe.id,
        job.id,
        _=user,  # type: ignore[arg-type]
        db=FakeDB(get_result=job),  # type: ignore[arg-type]
    )
    assert status["job"]["id"] == str(job.id)  # type: ignore[index]

    job.recipe_id = uuid.uuid4()
    with pytest.raises(HTTPException) as missing:
        media_api.image_generation_status(
            recipe.id,
            job.id,
            _=user,  # type: ignore[arg-type]
            db=FakeDB(get_result=job),  # type: ignore[arg-type]
        )
    assert missing.value.status_code == 404


def test_recipe_image_generation_api_rejects_unavailable_and_queues_regeneration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    recipe = _recipe()
    monkeypatch.setattr(media_api, "image_generation_available", lambda *_args: False)
    with pytest.raises(HTTPException) as unavailable:
        media_api.start_image_generation(  # type: ignore[arg-type]
            recipe.id, user=user, db=FakeDB()
        )
    assert unavailable.value.status_code == 503

    monkeypatch.setattr(media_api, "image_generation_available", lambda *_args: True)
    cover = SimpleNamespace(id=uuid.uuid4())
    recipe.images.append(cover)
    recipe.cover_image = cover
    monkeypatch.setattr(media_api, "get_recipe", lambda *_args, **_kwargs: recipe)
    monkeypatch.setattr(media_api, "get_active_image_generation_job", lambda *_args: None)
    send = Mock()
    monkeypatch.setattr(media_api.image_generation_task, "send", send)
    db = FakeDB()

    result = media_api.start_image_generation(  # type: ignore[arg-type]
        recipe.id, user=user, db=db
    )

    job = db.added[0]
    assert isinstance(job, ImageGenerationJob)
    assert job.generation_mode == "regenerate"
    assert job.previous_cover_image_id == cover.id
    assert result["job"]["generation_mode"] == "regenerate"  # type: ignore[index]
    assert "aktuellen Titelbild" in result["message"]
    send.assert_called_once_with(str(job.id))


def test_media_api_success_and_invalid_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user()
    recipe = _recipe()
    asset_path = tmp_path / "image.png"
    thumb_path = tmp_path / "thumb.webp"
    asset_path.write_bytes(b"image")
    thumb_path.write_bytes(b"thumb")
    image = SimpleNamespace(
        id=uuid.uuid4(),
        media_asset_id=uuid.uuid4(),
        position=0,
        is_cover=True,
        caption="Suppe",
        alt_text="Schüssel",
        asset=SimpleNamespace(storage_key="image.png"),
        thumbnail_asset=SimpleNamespace(storage_key="thumb.webp"),
    )
    monkeypatch.setattr(media_api, "get_recipe", lambda *_args, **_kwargs: recipe)
    monkeypatch.setattr(media_api, "add_recipe_image", AsyncMock(return_value=image))
    monkeypatch.setattr(
        media_api,
        "resolve_storage_key",
        lambda key: asset_path if key == "image.png" else thumb_path,
    )
    db = FakeDB()
    result = asyncio.run(
        media_api.upload_image(
            recipe.id,
            AsyncUpload(b"image", "image.png"),  # type: ignore[arg-type]
            caption="Suppe",
            alt_text="Schüssel",
            is_cover=True,
            user=user,
            db=db,
        )
    )
    assert result["image"]["is_cover"] is True

    monkeypatch.setattr(
        media_api, "add_recipe_image", AsyncMock(side_effect=InvalidUpload("Falscher Typ"))
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            media_api.upload_image(
                recipe.id,
                AsyncUpload(b"bad", "bad.exe"),  # type: ignore[arg-type]
                user=user,
                db=db,
            )
        )
    assert exc.value.status_code == 422 and db.rollbacks == 1


def test_media_api_metadata_delete_view_and_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user()
    recipe = _recipe()
    image = SimpleNamespace(id=uuid.uuid4(), recipe_id=recipe.id)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        storage_key="stored.bin",
        mime_type="image/png",
        original_filename="../../unsafe.png",
    )
    path = tmp_path / "stored.bin"
    path.write_bytes(b"image")
    db = FakeDB(scalar_values=[image, image])
    monkeypatch.setattr(media_api, "get_recipe", lambda *_args, **_kwargs: recipe)
    monkeypatch.setattr(media_api, "update_image", Mock())
    monkeypatch.setattr(media_api, "remove_image", lambda *_args: [asset])
    monkeypatch.setattr(media_api, "resolve_storage_key", lambda _key: path)
    assert (
        media_api.change_image(
            recipe.id, image.id, ImageMetadataInput(caption="Neu"), _=user, db=db
        )["message"]
        == "Bildangaben gespeichert"
    )
    assert media_api.delete_image(recipe.id, image.id, _=user, db=db)["message"].startswith("Bild")
    assert not path.exists()

    path.write_bytes(b"image")
    monkeypatch.setattr(media_api, "get_asset", lambda *_args: asset)
    monkeypatch.setattr(media_api, "asset_is_attached", lambda *_args: True)
    assert media_api.view_asset(asset.id, _=user, db=db).media_type == "image/png"
    download = media_api.download_asset(asset.id, _=user, db=db)
    assert download.media_type == "application/octet-stream"
    assert "unsafe.png" in download.headers["content-disposition"]

    monkeypatch.setattr(media_api, "asset_is_attached", lambda *_args: False)
    with pytest.raises(HTTPException) as exc:
        media_api.view_asset(asset.id, _=user, db=db)
    assert exc.value.status_code == 404
