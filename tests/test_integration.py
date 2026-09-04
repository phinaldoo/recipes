from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Generator
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from http.cookies import SimpleCookie
from io import BytesIO
from pathlib import Path
from queue import Queue
from typing import Never

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException, Request, Response
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, delete, func, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import maintenance
from app.ai.image_client import GeneratedRecipeImage
from app.api import auth as auth_api
from app.auth import security
from app.config import get_settings
from app.database import get_db
from app.imports.json_import import import_recipe_package
from app.models import (
    Category,
    ImageGenerationJob,
    MediaAsset,
    Recipe,
    RecipeComment,
    RecipeImage,
    RecipeOriginalAsset,
    RecipeShare,
    RecipeVersion,
    User,
    UserNote,
    UserSession,
)
from app.schemas.notes import NotePayload
from app.schemas.recipe import (
    CategoryPathInput,
    IngredientGroupInput,
    IngredientInput,
    InstructionStepInput,
    NutritionInput,
    RecipeInput,
    RecipePackage,
    SourceInput,
)
from app.services import comments as comment_service
from app.services import image_generation
from app.services import shares as shares_service
from app.services.comments import create_comment, delete_comment, update_comment
from app.services.exports import recipe_package_dict
from app.services.media import create_asset, create_thumbnail_asset
from app.services.notes import create_note, delete_note, get_note, list_notes, update_note
from app.services.productivity import rename_tag
from app.services.recipes import (
    RecipeConflict,
    create_recipe,
    get_recipe,
    list_recipes,
    restore_recipe,
    soft_delete_recipe,
    update_recipe,
)
from app.services.shares import create_share, resolve_share
from app.services.storage import store_bytes

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _skip_locally_or_fail_ci(message: str) -> Never:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"}:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


@pytest.fixture(scope="session")
def postgres_engine() -> Generator[Engine, None, None]:
    """Prepare the explicitly configured integration database once.

    An implicit development database is deliberately never modified. CI supplies
    DATABASE_URL; a developer can opt in locally by setting the same variable.
    """

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        _skip_locally_or_fail_ci(
            "PostgreSQL-Integrationstests benötigen eine gesetzte DATABASE_URL."
        )
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        _skip_locally_or_fail_ci("DATABASE_URL verweist nicht auf eine PostgreSQL-Datenbank.")

    engine = create_engine(database_url, poolclass=NullPool, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        _skip_locally_or_fail_ci(
            f"PostgreSQL ist für die Integrationstests nicht erreichbar: {exc}"
        )

    # Exercise the real, immutable migration chain before testing application data.
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(alembic_config, "head")

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db(postgres_engine: Engine) -> Generator[Session, None, None]:
    """Run a test in an outer transaction, including code that calls commit()."""

    connection = postgres_engine.connect()
    outer_transaction = connection.begin()
    session = Session(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        session.close()
        if outer_transaction.is_active:
            outer_transaction.rollback()
        connection.close()


def _user(db: Session, name: str, *, role: str = "member") -> User:
    unique = uuid.uuid4().hex
    user = User(
        email=f"integration-{name.casefold()}-{unique}@example.test",
        display_name=name,
        password_hash="integration-test-password-hash",
        role=role,
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def _auth_request(user_agent: str, token: str | None = None) -> Request:
    headers = [(b"user-agent", user_agent.encode())]
    if token is not None:
        headers.append((b"cookie", f"{get_settings().session_cookie_name}={token}".encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/auth/login",
            "raw_path": b"/api/v1/auth/login",
            "query_string": b"",
            "headers": headers,
            "client": ("192.0.2.10", 4242),
            "server": ("example.test", 443),
        }
    )


def _session_token(response: Response) -> str:
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    return cookies[get_settings().session_cookie_name].value


def test_same_user_can_use_parallel_device_sessions(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db, "Mehrere Geräte")
    payload = auth_api.LoginPayload(email=user.email, password="correct-test-password")
    monkeypatch.setattr(auth_api, "check_login_rate_limit", lambda *_args: None)
    monkeypatch.setattr(auth_api, "verify_password", lambda *_args: True)
    monkeypatch.setattr(auth_api, "password_needs_rehash", lambda *_args: False)

    device_a_response = Response()
    auth_api.login(payload, _auth_request("Device A"), device_a_response, db)
    device_a_token = _session_token(device_a_response)

    device_b_response = Response()
    auth_api.login(payload, _auth_request("Device B"), device_b_response, db)
    device_b_token = _session_token(device_b_response)

    sessions = list(
        db.scalars(
            select(UserSession)
            .where(UserSession.user_id == user.id)
            .order_by(UserSession.created_at, UserSession.id)
        )
    )
    assert len(sessions) == 2
    assert device_a_token != device_b_token

    device_a_session = security.get_session(db, _auth_request("Device A", device_a_token))
    device_b_session = security.get_session(db, _auth_request("Device B", device_b_token))
    assert device_a_session is not None
    assert device_b_session is not None
    assert device_a_session.id != device_b_session.id

    security.delete_session(
        db,
        _auth_request("Device A", device_a_token),
        Response(),
    )
    db.commit()

    assert security.get_session(db, _auth_request("Device A", device_a_token)) is None
    assert security.get_session(db, _auth_request("Device B", device_b_token)) is not None
    assert (
        db.scalar(
            select(func.count()).select_from(UserSession).where(UserSession.user_id == user.id)
        )
        == 1
    )


def _payload(
    title: str,
    *,
    categories: list[CategoryPathInput] | None = None,
    expected_updated_at: datetime | None = None,
    status: str = "active",
) -> RecipeInput:
    return RecipeInput(
        title=title,
        description="Ein vollständiges Integrationstest-Rezept.",
        base_servings=Decimal("4"),
        serving_label="Personen",
        prep_time_minutes=10,
        cook_time_minutes=25,
        rest_time_minutes=5,
        nutrition=[
            NutritionInput(
                basis="per_serving",
                energy_kj=Decimal("1451"),
                energy_kcal=Decimal("347"),
                fat_g=Decimal("13"),
                carbohydrates_g=Decimal("47"),
                protein_g=Decimal("10"),
                note="Eine Portion entspricht einem Viertel des Rezepts.",
            )
        ],
        notes="Schmeckt am nächsten Tag noch besser.",
        status=status,
        ingredient_groups=[
            IngredientGroupInput(
                title="Teig",
                ingredients=[
                    IngredientInput(
                        amount_min=Decimal("1.5"),
                        amount_max=Decimal("2.0"),
                        unit="kg",
                        name="Kartoffeln",
                        note="mehligkochend",
                    ),
                    IngredientInput(
                        amount_min=Decimal("1"),
                        unit="Prise",
                        name="Salz",
                        is_scalable=False,
                    ),
                ],
            )
        ],
        instruction_steps=[
            InstructionStepInput(text="Kartoffeln garen."),
            InstructionStepInput(text="Alles sorgfältig vermengen."),
        ],
        categories=categories or [],
        source=SourceInput(title="Familienkochbuch", url="https://example.test/rezept"),
        expected_updated_at=expected_updated_at,
    )


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 18), color=(54, 106, 77)).save(output, format="PNG")
    return output.getvalue()


def _pdf_bytes() -> bytes:
    content = b"BT /F1 12 Tf 20 50 Td (Originalrezept) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(document)


def test_migration_head_and_postgresql_schema_assumptions(postgres_engine: Engine) -> None:
    inspector = inspect(postgres_engine)
    table_names = set(inspector.get_table_names())
    assert {
        "alembic_version",
        "users",
        "recipes",
        "categories",
        "recipe_categories",
        "ingredient_groups",
        "ingredients",
        "instruction_steps",
        "recipe_comments",
        "media_assets",
        "recipe_images",
        "recipe_original_assets",
        "recipe_nutrition",
        "recipe_versions",
        "image_generation_jobs",
        "import_candidates",
        "user_notes",
    } <= table_names
    assert {"shopping_list_items", "meal_plan_entries"}.isdisjoint(table_names)

    with postgres_engine.connect() as connection:
        database_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        extensions = set(
            connection.scalars(
                text("SELECT extname FROM pg_extension WHERE extname IN ('unaccent', 'pg_trgm')")
            )
        )
    migration_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    migration_config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    assert database_revision == ScriptDirectory.from_config(migration_config).get_current_head()
    assert extensions == {"unaccent", "pg_trgm"}

    recipe_columns = {column["name"] for column in inspector.get_columns("recipes")}
    assert {"created_by_name_snapshot", "updated_by_name_snapshot", "search_vector"} <= (
        recipe_columns
    )
    image_columns = {column["name"] for column in inspector.get_columns("recipe_images")}
    assert {"thumbnail_asset_id", "generation_metadata"} <= image_columns

    image_indexes = {index["name"]: index for index in inspector.get_indexes("recipe_images")}
    assert image_indexes["uq_recipe_images_single_cover"]["unique"] is True
    assert "postgresql_where" in image_indexes["uq_recipe_images_single_cover"]["dialect_options"]

    generation_indexes = {
        index["name"]: index for index in inspector.get_indexes("image_generation_jobs")
    }
    generation_columns = {
        column["name"] for column in inspector.get_columns("image_generation_jobs")
    }
    assert {"generation_mode", "previous_cover_image_id"} <= generation_columns
    assert generation_indexes["uq_image_generation_jobs_active_recipe"]["unique"] is True
    assert (
        "postgresql_where"
        in generation_indexes["uq_image_generation_jobs_active_recipe"]["dialect_options"]
    )

    candidate_columns = {column["name"] for column in inspector.get_columns("import_candidates")}
    assert {
        "job_id",
        "position",
        "recipe_payload",
        "source_regions_json",
        "image_region_json",
        "image_asset_id",
        "thumbnail_asset_id",
        "result_recipe_id",
    } <= candidate_columns
    import_job_checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("import_jobs")
    }
    assert "review" in import_job_checks["ck_import_jobs_status"]


def test_user_notes_are_private_and_support_crud(db: Session) -> None:
    owner = _user(db, "Notizbesitzerin")
    other_user = _user(db, "Andere Person")
    note = create_note(
        db,
        owner,
        NotePayload(
            title="Zitronenpasta",
            url="https://example.test/rezepte/zitronenpasta",
            content="Am Wochenende ausprobieren.",
        ),
    )
    db.flush()

    assert [item.id for item in list_notes(db, owner)] == [note.id]
    assert list_notes(db, other_user) == []
    with pytest.raises(HTTPException) as hidden:
        get_note(db, other_user, note.id)
    assert hidden.value.status_code == 404

    update_note(
        db,
        get_note(db, owner, note.id),
        NotePayload(url="https://example.test/rezepte/zitronenpasta-neu"),
    )
    assert note.title is None
    assert note.content is None
    assert note.url == "https://example.test/rezepte/zitronenpasta-neu"

    delete_note(db, get_note(db, owner, note.id))
    assert db.get(UserNote, note.id) is None


def test_recipe_image_generation_worker_persists_generated_cover(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    settings.ensure_directories()
    user = _user(db, "Bildauftrag")
    recipe = create_recipe(db, _payload(f"Kartoffelgericht {uuid.uuid4().hex}"), user)
    job = ImageGenerationJob(
        recipe_id=recipe.id,
        requested_by_user_id=user.id,
        generation_mode="create",
        status="queued",
        current_stage="Wartet auf Bildgenerierung",
        attempt_count=0,
    )
    db.add(job)
    db.flush()
    prompts: list[str] = []

    def generate(prompt: str, **_kwargs: object) -> GeneratedRecipeImage:
        prompts.append(prompt)
        return GeneratedRecipeImage(_png_bytes(), "Refined food photograph")

    monkeypatch.setattr(image_generation, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(image_generation, "get_settings", lambda: settings)
    monkeypatch.setattr(image_generation, "generate_recipe_image", generate)

    image_generation._process_image_generation_job(job.id)

    db.expire_all()
    completed = db.get(ImageGenerationJob, job.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.attempt_count == 1
    assert completed.result_image is not None
    assert completed.result_image.is_cover is True
    assert completed.result_image.asset.kind == "generated_image"
    assert completed.result_image.generation_metadata is not None
    assert completed.result_image.generation_metadata["model"] == settings.ai_image_model
    assert completed.result_image.generation_metadata["revised_prompt"] == "Refined food photograph"
    assert image_generation.resolve_storage_key(completed.result_image.asset.storage_key).is_file()
    assert len(prompts) == 1
    assert recipe.title in prompts[0]
    assert "Kartoffeln" in prompts[0]
    assert "Schmeckt am nächsten Tag" not in prompts[0]


def test_recipe_image_regeneration_sends_and_retains_previous_cover(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    settings.ensure_directories()
    user = _user(db, "Bildregeneration")
    recipe = create_recipe(db, _payload(f"Kartoffelauflauf {uuid.uuid4().hex}"), user)
    previous_bytes = _png_bytes()
    stored = store_bytes(previous_bytes, filename="bisheriges-bild.png", kind="recipe_image")
    previous_asset = create_asset(db, stored, user, "recipe_image")
    previous_thumbnail = create_thumbnail_asset(db, previous_asset, user)
    previous = RecipeImage(
        recipe_id=recipe.id,
        asset=previous_asset,
        thumbnail_asset=previous_thumbnail,
        position=0,
        is_cover=True,
        alt_text="Bisheriges Titelbild",
    )
    recipe.images.append(previous)
    db.flush()
    job = ImageGenerationJob(
        recipe_id=recipe.id,
        requested_by_user_id=user.id,
        previous_cover_image_id=previous.id,
        generation_mode="regenerate",
        status="queued",
        current_stage="Wartet auf neues Rezeptbild",
        attempt_count=0,
    )
    db.add(job)
    db.flush()
    edit_calls: list[tuple[str, bytes, str]] = []

    def edit(
        prompt: str,
        reference: bytes,
        reference_mime: str,
        **_kwargs: object,
    ) -> GeneratedRecipeImage:
        edit_calls.append((prompt, reference, reference_mime))
        return GeneratedRecipeImage(_png_bytes(), "Improved from current cover")

    monkeypatch.setattr(image_generation, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(image_generation, "get_settings", lambda: settings)
    monkeypatch.setattr(image_generation, "edit_recipe_image", edit)

    image_generation._process_image_generation_job(job.id)

    db.expire_all()
    completed = db.get(ImageGenerationJob, job.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.current_stage == "Neues Rezeptbild wurde erstellt"
    assert completed.result_image is not None
    images = list(
        db.scalars(
            select(RecipeImage)
            .where(RecipeImage.recipe_id == recipe.id)
            .order_by(RecipeImage.position)
        )
    )
    assert len(images) == 2
    assert images[0].id == previous.id and images[0].is_cover is False
    assert images[1].id == completed.result_image.id and images[1].is_cover is True
    assert image_generation.resolve_storage_key(images[0].asset.storage_key).is_file()
    assert len(edit_calls) == 1
    prompt, reference, reference_mime = edit_calls[0]
    assert reference == previous_bytes
    assert reference_mime == "image/png"
    assert "bereitgestellte Bild" in prompt
    assert recipe.title in prompt
    assert "Schmeckt am nächsten Tag" not in prompt
    assert completed.result_image.generation_metadata is not None
    assert completed.result_image.generation_metadata["previous_cover_image_id"] == str(previous.id)


def test_restore_advisory_barrier_drains_shared_work_and_excludes_new_writes(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "engine", postgres_engine)

    with maintenance.database_maintenance_shared_guard(), postgres_engine.connect() as contender:
        assert (
            contender.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": maintenance.MAINTENANCE_ADVISORY_LOCK},
            )
            is False
        )

    with maintenance.database_maintenance_exclusive_guard(), postgres_engine.connect() as contender:
        assert (
            contender.scalar(
                text("SELECT pg_try_advisory_lock_shared(:lock_id)"),
                {"lock_id": maintenance.MAINTENANCE_ADVISORY_LOCK},
            )
            is False
        )


def test_request_database_commit_survives_the_session_advisory_barrier(
    postgres_engine: Engine,
) -> None:
    identifier = uuid.uuid4()
    database_dependency = get_db()
    request_db = next(database_dependency)
    try:
        request_db.add(
            User(
                id=identifier,
                email=f"barrier-{identifier}@example.test",
                password_hash="integration-test-password-hash",
                role="member",
                is_active=True,
            )
        )
        request_db.commit()
    finally:
        with pytest.raises(StopIteration):
            next(database_dependency)

    with Session(bind=postgres_engine) as verification:
        assert verification.get(User, identifier) is not None
        verification.execute(delete(User).where(User.id == identifier))
        verification.commit()


def test_two_users_share_the_same_global_recipe_collection(db: Session) -> None:
    author = _user(db, "Anna")
    viewer = _user(db, "Bernd")
    recipe = create_recipe(db, _payload(f"Global {uuid.uuid4().hex}"), author)
    db.flush()

    # The collection query intentionally has no owner/user predicate. Both signed-in
    # identities therefore resolve the same globally shared data set.
    visible_by_identity: dict[uuid.UUID, set[uuid.UUID]] = {}
    for identity in (author, viewer):
        recipes, total, pages, effective_page = list_recipes(
            db, q=recipe.title, page=1, page_size=24
        )
        visible_by_identity[identity.id] = {item.id for item in recipes}
        assert total == 1
        assert pages == 1
        assert effective_page == 1

    assert recipe.id in visible_by_identity[author.id]
    assert visible_by_identity[author.id] == visible_by_identity[viewer.id]
    assert recipe.created_by_user_id == author.id
    assert recipe.created_by_user_id != viewer.id


def test_recipe_crud_and_exact_optimistic_lock(db: Session) -> None:
    creator = _user(db, "Clara")
    editor = _user(db, "David")
    recipe = create_recipe(db, _payload(f"CRUD {uuid.uuid4().hex}"), creator)
    db.flush()
    recipe_id = recipe.id
    original_updated_at = recipe.updated_at

    loaded = get_recipe(db, recipe_id)
    assert loaded.title == recipe.title
    assert loaded.total_time_minutes == 40
    assert loaded.nutrition_per_serving is not None
    assert loaded.nutrition_per_serving.energy_kcal == Decimal("347")
    assert loaded.nutrition_per_100g_ml is None
    assert loaded.created_by_name_snapshot == "Clara"

    updated_payload = _payload(
        f"Aktualisiert {uuid.uuid4().hex}", expected_updated_at=original_updated_at
    )
    update_recipe(db, get_recipe(db, recipe_id, for_update=True), updated_payload, editor)
    db.flush()
    changed = get_recipe(db, recipe_id)
    assert changed.title == updated_payload.title
    assert changed.updated_by_user_id == editor.id
    assert changed.updated_by_name_snapshot == "David"
    assert changed.updated_at > original_updated_at
    assert (
        db.scalar(
            select(RecipeVersion.version_number)
            .where(RecipeVersion.recipe_id == recipe_id)
            .order_by(RecipeVersion.version_number.desc())
            .limit(1)
        )
        == 2
    )

    stale_payload = _payload("Veralteter Schreibversuch", expected_updated_at=original_updated_at)
    with pytest.raises(RecipeConflict, match="inzwischen"):
        update_recipe(db, get_recipe(db, recipe_id, for_update=True), stale_payload, creator)

    soft_delete_recipe(db, changed, editor)
    with pytest.raises(HTTPException) as missing:
        get_recipe(db, recipe_id)
    assert missing.value.status_code == 404
    deleted = get_recipe(db, recipe_id, include_deleted=True)
    assert deleted.deleted_at is not None

    restore_recipe(db, deleted, creator)
    restored = get_recipe(db, recipe_id)
    assert restored.deleted_at is None
    assert restored.updated_by_user_id == creator.id


def test_recipe_delete_and_archive_permanently_revoke_share_links(db: Session) -> None:
    user = _user(db, "Freigabe")
    deleted_recipe = create_recipe(db, _payload(f"Loeschen {uuid.uuid4().hex}"), user)
    _, deleted_token = create_share(db, user, deleted_recipe.id, None)
    db.flush()

    soft_delete_recipe(db, deleted_recipe, user)
    restore_recipe(db, deleted_recipe, user)
    with pytest.raises(HTTPException) as deleted_share:
        resolve_share(db, deleted_token)
    assert deleted_share.value.status_code == 404

    archived_recipe = create_recipe(db, _payload(f"Archivieren {uuid.uuid4().hex}"), user)
    _, archived_token = create_share(db, user, archived_recipe.id, None)
    db.flush()
    update_recipe(
        db,
        archived_recipe,
        _payload(
            archived_recipe.title,
            expected_updated_at=archived_recipe.updated_at,
            status="archived",
        ),
        user,
    )
    update_recipe(
        db,
        archived_recipe,
        _payload(
            archived_recipe.title,
            expected_updated_at=archived_recipe.updated_at,
            status="active",
        ),
        user,
    )
    with pytest.raises(HTTPException) as archived_share:
        resolve_share(db, archived_token)
    assert archived_share.value.status_code == 404


def test_share_creation_serializes_with_recipe_deletion(
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(bind=postgres_engine, expire_on_commit=False) as setup:
        user = _user(setup, "Share-Race")
        recipe = create_recipe(setup, _payload(f"Share Race {uuid.uuid4().hex}"), user)
        setup.commit()
        user_id = user.id
        recipe_id = recipe.id

    entered_share_lookup = threading.Event()
    outcome: Queue[int | str] = Queue()
    original_get_recipe = shares_service._get_recipe_for_share_creation

    def signaling_get_recipe(*args: object, **kwargs: object) -> Recipe:
        entered_share_lookup.set()
        return original_get_recipe(*args, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(shares_service, "_get_recipe_for_share_creation", signaling_get_recipe)

    def create_concurrently() -> None:
        with Session(bind=postgres_engine) as concurrent:
            concurrent_user = concurrent.get(User, user_id)
            assert concurrent_user is not None
            try:
                create_share(concurrent, concurrent_user, recipe_id, None)
                concurrent.commit()
                outcome.put("created")
            except HTTPException as exc:
                concurrent.rollback()
                outcome.put(exc.status_code)

    try:
        with Session(bind=postgres_engine) as deleting:
            locked_recipe = get_recipe(deleting, recipe_id, for_update=True)
            deleting_user = deleting.get(User, user_id)
            assert deleting_user is not None
            soft_delete_recipe(deleting, locked_recipe, deleting_user)
            deleting.flush()

            worker = threading.Thread(target=create_concurrently, daemon=True)
            worker.start()
            assert entered_share_lookup.wait(timeout=5)
            deleting.commit()

        worker.join(timeout=5)
        assert not worker.is_alive()
        assert outcome.get_nowait() == 404

        with Session(bind=postgres_engine) as restoring:
            deleted = get_recipe(restoring, recipe_id, include_deleted=True, for_update=True)
            restoring_user = restoring.get(User, user_id)
            assert restoring_user is not None
            restore_recipe(restoring, deleted, restoring_user)
            restoring.commit()
            assert (
                restoring.scalar(
                    select(func.count())
                    .select_from(RecipeShare)
                    .where(RecipeShare.recipe_id == recipe_id)
                )
                == 0
            )
    finally:
        with Session(bind=postgres_engine) as cleanup:
            stored_recipe = cleanup.get(Recipe, recipe_id)
            if stored_recipe is not None:
                cleanup.delete(stored_recipe)
            stored_user = cleanup.get(User, user_id)
            if stored_user is not None:
                cleanup.delete(stored_user)
            cleanup.commit()


def test_global_tag_rename_invalidates_stale_recipe_forms_and_creates_version(
    db: Session,
) -> None:
    user = _user(db, "Schlagwort")
    recipe = create_recipe(
        db,
        _payload(f"Tag {uuid.uuid4().hex}").model_copy(update={"tags": ["Saisonal"]}),
        user,
    )
    db.flush()
    stale_updated_at = recipe.updated_at
    tag = recipe.tags[0]

    rename_tag(db, tag.id, "Jahreszeitlich", user)
    db.flush()

    assert recipe.updated_at > stale_updated_at
    latest = db.scalar(
        select(RecipeVersion)
        .where(RecipeVersion.recipe_id == recipe.id)
        .order_by(RecipeVersion.version_number.desc())
        .limit(1)
    )
    assert latest is not None
    assert latest.change_summary == "Schlagwort in „Jahreszeitlich“ umbenannt"
    with pytest.raises(RecipeConflict, match="inzwischen"):
        update_recipe(
            db,
            recipe,
            _payload(recipe.title, expected_updated_at=stale_updated_at).model_copy(
                update={"tags": ["Saisonal"]}
            ),
            user,
        )


def test_category_hierarchy_and_twenty_category_limit(db: Session) -> None:
    user = _user(db, "Erika")
    categories = [
        CategoryPathInput(path=["Integration", f"Kategorie {index:02d}"], origin="manual")
        for index in range(20)
    ]
    recipe = create_recipe(
        db,
        _payload(f"Zwanzig Kategorien {uuid.uuid4().hex}", categories=categories),
        user,
    )
    db.flush()

    assert len(recipe.categories) == 20
    assert {category.path for category in recipe.categories} == {
        f"Integration › Kategorie {index:02d}" for index in range(20)
    }
    roots = list(
        db.scalars(
            select(Category).where(
                Category.parent_id.is_(None), Category.normalized_name == "integration"
            )
        )
    )
    assert len(roots) == 1

    too_many = [
        {"path": ["Grenzwert", f"Kategorie {index:02d}"], "origin": "manual"} for index in range(21)
    ]
    with pytest.raises(ValidationError, match="at most 20 items"):
        RecipeInput.model_validate(
            {
                "title": "Eine Kategorie zu viel",
                "base_servings": "4",
                "categories": too_many,
            }
        )

    # Also exercise the service-side guard in case a validated model is mutated.
    guarded_payload = _payload("Service-Grenzwert", categories=list(categories))
    guarded_payload.categories.append(
        CategoryPathInput(path=["Integration", "Kategorie 20"], origin="manual")
    )
    with pytest.raises(ValueError, match="höchstens 20 Kategorien"):
        create_recipe(db, guarded_payload, user)


def test_recipe_display_categories_expand_ancestors_without_duplicates(db: Session) -> None:
    user = _user(db, "Kategorienanzeige")
    suffix = uuid.uuid4().hex
    baking = f"Backen-{suffix}"
    desserts = f"Desserts-{suffix}"
    recipe = create_recipe(
        db,
        _payload(
            f"Kategorienanzeige {suffix}",
            categories=[
                CategoryPathInput(path=[baking, "Kuchen"]),
                CategoryPathInput(path=[baking, "Kuchen", "Nusskuchen"]),
                CategoryPathInput(path=[baking, "Kuchen", "Kuchen mit Alkohol"]),
                CategoryPathInput(path=[desserts, "Eierlikör"]),
                CategoryPathInput(path=[baking, "Kuchen", "Schokoladenkuchen"]),
            ],
        ),
        user,
    )
    db.flush()
    recipe_id = recipe.id
    db.expire_all()

    loaded = get_recipe(db, recipe_id)

    assert [category.name for category in loaded.expanded_categories] == [
        baking,
        "Kuchen",
        "Nusskuchen",
        "Kuchen mit Alkohol",
        desserts,
        "Eierlikör",
        "Schokoladenkuchen",
    ]


def test_recipe_category_filter_includes_the_selected_category_subtree(db: Session) -> None:
    user = _user(db, "Filter")
    suffix = uuid.uuid4().hex
    first_root = f"Filter-A-{suffix}"
    second_root = f"Filter-B-{suffix}"

    direct = create_recipe(
        db,
        _payload(
            f"Direkt {suffix}",
            categories=[CategoryPathInput(path=[first_root])],
        ),
        user,
    )
    child = create_recipe(
        db,
        _payload(
            f"Kind {suffix}",
            categories=[CategoryPathInput(path=[first_root, "Kind"])],
        ),
        user,
    )
    grandchild = create_recipe(
        db,
        _payload(
            f"Enkel {suffix}",
            categories=[CategoryPathInput(path=[first_root, "Kind", "Enkel"])],
        ),
        user,
    )
    sibling = create_recipe(
        db,
        _payload(
            f"Geschwister {suffix}",
            categories=[CategoryPathInput(path=[first_root, "Geschwister"])],
        ),
        user,
    )
    combined = create_recipe(
        db,
        _payload(
            f"Kombiniert {suffix}",
            categories=[
                CategoryPathInput(path=[first_root, "Kind"]),
                CategoryPathInput(path=[second_root, "Zweig", "Blatt"]),
            ],
        ),
        user,
    )
    second_direct = create_recipe(
        db,
        _payload(
            f"Zweite Wurzel {suffix}",
            categories=[CategoryPathInput(path=[second_root])],
        ),
        user,
    )
    db.flush()

    first_root_id = direct.categories[0].id
    child_id = child.categories[0].id
    second_root_id = second_direct.categories[0].id

    root_matches, _, _, _ = list_recipes(
        db,
        category_ids=[first_root_id],
        page_size=100,
    )
    assert {recipe.id for recipe in root_matches} == {
        direct.id,
        child.id,
        grandchild.id,
        sibling.id,
        combined.id,
    }

    child_matches, _, _, _ = list_recipes(
        db,
        category_ids=[child_id],
        page_size=100,
    )
    assert {recipe.id for recipe in child_matches} == {
        child.id,
        grandchild.id,
        combined.id,
    }

    combined_matches, _, _, _ = list_recipes(
        db,
        category_ids=[first_root_id, second_root_id],
        page_size=100,
    )
    assert {recipe.id for recipe in combined_matches} == {combined.id}

    redundant_parent_matches, _, _, _ = list_recipes(
        db,
        category_ids=[first_root_id, child_id],
        page_size=100,
    )
    assert {recipe.id for recipe in redundant_parent_matches} == {
        child.id,
        grandchild.id,
        combined.id,
    }


def test_recipe_kind_filter_separates_cooking_and_baking(db: Session) -> None:
    user = _user(db, "Rezeptart")
    suffix = uuid.uuid4().hex
    cooking = create_recipe(
        db,
        _payload(f"Kochen {suffix}").model_copy(update={"recipe_kind": "cooking"}),
        user,
    )
    baking = create_recipe(
        db,
        _payload(f"Backen {suffix}").model_copy(update={"recipe_kind": "baking"}),
        user,
    )
    db.flush()

    all_matches, all_total, _, _ = list_recipes(db, q=suffix, page_size=100)
    cooking_matches, cooking_total, _, _ = list_recipes(
        db,
        q=suffix,
        recipe_kind="cooking",
        page_size=100,
    )
    baking_matches, baking_total, _, _ = list_recipes(
        db,
        q=suffix,
        recipe_kind="baking",
        page_size=100,
    )

    assert {recipe.id for recipe in all_matches} == {cooking.id, baking.id}
    assert all_total == 2
    assert [recipe.id for recipe in cooking_matches] == [cooking.id]
    assert cooking_total == 1
    assert [recipe.id for recipe in baking_matches] == [baking.id]
    assert baking_total == 1


def test_comment_author_and_admin_permissions(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comment_service, "_rate_limit", lambda *_args: None)
    author = _user(db, "Fatima")
    other_member = _user(db, "Gregor")
    admin = _user(db, "Helena", role="admin")
    recipe = create_recipe(db, _payload(f"Notizen {uuid.uuid4().hex}"), author)

    comment = create_comment(db, recipe, author, "  Meine private Notiz  ")
    assert comment.text == "Meine private Notiz"
    assert comment.author_user_id == author.id
    assert comment.author_name_snapshot == "Fatima"

    with pytest.raises(HTTPException) as forbidden_update:
        update_comment(db, recipe, comment, other_member, "Fremde Änderung")
    assert forbidden_update.value.status_code == 403
    with pytest.raises(HTTPException) as forbidden_admin_update:
        update_comment(db, recipe, comment, admin, "Admin-Änderung")
    assert forbidden_admin_update.value.status_code == 403

    update_comment(db, recipe, comment, author, "Eigene Änderung")
    assert comment.text == "Eigene Änderung"
    author.display_name = "Neuer Anzeigename"
    db.flush()
    assert comment.author_name_snapshot == "Fatima"

    with pytest.raises(HTTPException) as forbidden_delete:
        delete_comment(db, recipe, comment, other_member)
    assert forbidden_delete.value.status_code == 403
    delete_comment(db, recipe, comment, admin)
    assert comment.deleted_at is not None

    own_comment = create_comment(db, recipe, author, "Noch eine Notiz")
    delete_comment(db, recipe, own_comment, author)
    assert own_comment.deleted_at is not None


def test_recipe_json_roundtrip_preserves_content_files_and_metadata(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    settings.ensure_directories()

    author = _user(db, "Historische Autorin")
    importer = _user(db, "Importierender Nutzer")
    recipe = create_recipe(
        db,
        _payload(
            f"Roundtrip {uuid.uuid4().hex}",
            categories=[CategoryPathInput(path=["Saisonal", "Herbst"], origin="ai_import")],
        ),
        author,
    )

    image_bytes = _png_bytes()
    stored_image = store_bytes(
        image_bytes,
        filename="gericht.png",
        kind="generated_image",
    )
    image_asset = create_asset(db, stored_image, author, "generated_image")
    thumbnail_asset = create_thumbnail_asset(db, image_asset, author)
    recipe.images.append(
        RecipeImage(
            media_asset_id=image_asset.id,
            thumbnail_asset_id=thumbnail_asset.id if thumbnail_asset else None,
            position=0,
            is_cover=True,
            caption="Goldbraun serviert",
            alt_text="Eine grüne Auflaufform auf einem Holztisch",
            generation_metadata={
                "provider": "openai",
                "model": "gpt-image-1",
                "prompt": "Herbstliches Kartoffelgericht, natürliches Licht",
                "revised_prompt": "Editorial food photograph in soft daylight",
            },
        )
    )

    stored_original = store_bytes(
        _pdf_bytes(),
        filename="familienrezept.pdf",
        kind="original_upload",
    )
    original_asset = create_asset(db, stored_original, author, "original_upload")
    recipe.original_assets.append(RecipeOriginalAsset(media_asset_id=original_asset.id, position=0))

    comment_created_at = datetime(2024, 10, 5, 12, 30, tzinfo=UTC)
    comment_updated_at = datetime(2024, 10, 6, 8, 15, tzinfo=UTC)
    recipe.comments.append(
        RecipeComment(
            author_user_id=author.id,
            author_name_snapshot="Historische Autorin",
            text="Beim nächsten Mal etwas mehr Muskat.",
            created_at=comment_created_at,
            updated_at=comment_updated_at,
        )
    )
    original_created_at = datetime(2023, 9, 1, 7, 0, tzinfo=UTC)
    original_updated_at = datetime(2024, 10, 6, 8, 15, tzinfo=UTC)
    recipe.created_at = original_created_at
    recipe.updated_at = original_updated_at
    recipe.created_by_name_snapshot = "Historische Autorin"
    recipe.updated_by_name_snapshot = "Redaktion Familienkochbuch"
    db.flush()
    db.expire_all()

    exported = recipe_package_dict(get_recipe(db, recipe.id), include_originals=True)
    package = RecipePackage.model_validate(exported)
    assert package.schema_version == "1.3"
    assert package.recipe.recipe_kind == recipe.recipe_kind
    assert package.recipe.created_by_name == "Historische Autorin"
    assert package.recipe.updated_by_name == "Redaktion Familienkochbuch"
    assert package.recipe.categories[0].origin == "ai_import"
    assert package.recipe.nutrition[0].energy_kcal == Decimal("347")
    assert package.recipe.comments[0].author_email is None
    assert package.recipe.images[0].generation_metadata == {
        "provider": "openai",
        "model": "gpt-image-1",
        "prompt": "Herbstliches Kartoffelgericht, natürliches Licht",
        "revised_prompt": "Editorial food photograph in soft daylight",
    }

    imported = import_recipe_package(db, package, importer)
    imported_id = imported.id
    db.expire_all()
    imported = get_recipe(db, imported_id)
    assert imported.id != recipe.id
    assert imported.title == recipe.title
    assert imported.recipe_kind == recipe.recipe_kind
    assert imported.created_by_name_snapshot == "Historische Autorin"
    assert imported.updated_by_name_snapshot == "Redaktion Familienkochbuch"
    assert imported.created_at == original_created_at
    assert imported.updated_at == original_updated_at
    assert imported.source is not None
    assert imported.source.title == "Familienkochbuch"
    assert imported.source.url == "https://example.test/rezept"
    assert imported.nutrition_per_serving is not None
    assert imported.nutrition_per_serving.protein_g == Decimal("10")
    assert imported.nutrition_per_serving.note == (
        "Eine Portion entspricht einem Viertel des Rezepts."
    )
    assert [category.path for category in imported.categories] == ["Saisonal › Herbst"]
    assert imported.categories[0].origin == "ai_import"

    assert len(imported.images) == 1
    imported_image = imported.images[0]
    assert imported_image.is_cover is True
    assert imported_image.caption == "Goldbraun serviert"
    assert imported_image.alt_text == "Eine grüne Auflaufform auf einem Holztisch"
    assert imported_image.generation_metadata == package.recipe.images[0].generation_metadata
    assert imported_image.asset.sha256 == package.recipe.images[0].sha256
    assert imported_image.thumbnail_asset is not None

    assert len(imported.original_assets) == 1
    assert imported.original_assets[0].asset.sha256 == package.recipe.original_assets[0].sha256
    assert imported.original_assets[0].asset.original_filename == "familienrezept.pdf"
    assert len(imported.comments) == 1
    assert imported.comments[0].author_name_snapshot == "Historische Autorin"
    assert imported.comments[0].author_user_id is None
    assert imported.comments[0].text == "Beim nächsten Mal etwas mehr Muskat."
    assert imported.comments[0].created_at == comment_created_at
    assert imported.comments[0].updated_at == comment_updated_at

    # A second export is the strongest roundtrip assertion: all portable recipe
    # fields and asset hashes must remain byte-for-byte equivalent.
    reexported = recipe_package_dict(imported, include_originals=True)
    assert reexported["recipe"] == exported["recipe"]
    assert db.scalar(select(MediaAsset).where(MediaAsset.id == imported_image.asset.id)) is not None
    assert db.scalar(select(Recipe).where(Recipe.id == imported_id)) is not None
