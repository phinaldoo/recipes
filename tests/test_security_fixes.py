from __future__ import annotations

import base64
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.api import exports as exports_api
from app.config import Settings
from app.imports import json_import
from app.models import MediaAsset, User
from app.schemas.recipe import RecipePackage
from app.services import exports, media_quota, storage
from app.services.media_quota import MediaQuotaExceeded
from app.services.storage import StorageCapacityExceeded


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "storage_root": tmp_path / "storage",
        "backup_temp_root": tmp_path / "backups",
        "storage_min_free_mb": 0,
    }
    values.update(overrides)
    return Settings(**values)


class _OneResult:
    def __init__(self, row: tuple[int, int]) -> None:
        self.row = row

    def one(self) -> tuple[int, int]:
        return self.row


class QuotaDB:
    def __init__(self, rows: list[tuple[int, int]], *, dialect: str = "postgresql") -> None:
        self.rows = list(rows)
        self.dialect = dialect
        self.executed: list[tuple[Any, Any]] = []

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, statement: Any, params: Any = None) -> Any:
        self.executed.append((statement, params))
        if params is not None:
            return SimpleNamespace()
        return _OneResult(self.rows.pop(0))


def test_asset_quota_is_transaction_locked_and_checks_global_then_user(tmp_path: Path) -> None:
    mib = 1024 * 1024
    settings = settings_for(
        tmp_path,
        media_recipe_max_count=1,
        media_recipe_max_mb=1,
        media_user_max_count=2,
        media_user_max_mb=1,
        media_global_max_count=3,
        media_global_max_mb=2,
    )
    db = QuotaDB([(1, 900_000), (1, 900_000)])
    with pytest.raises(MediaQuotaExceeded, match="persönliche"):
        media_quota.enforce_new_asset_quota(
            db,  # type: ignore[arg-type]
            user_id=uuid.uuid4(),
            byte_size=mib // 2,
            settings=settings,
        )
    assert db.executed[0][1] == {"lock_id": media_quota.MEDIA_QUOTA_LOCK_ID}
    assert len(db.executed) == 3


def test_recipe_quota_counts_images_thumbnails_and_originals(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        media_recipe_max_count=2,
        media_recipe_max_mb=1,
        media_user_max_count=10,
        media_user_max_mb=10,
        media_global_max_count=20,
        media_global_max_mb=20,
    )
    db = QuotaDB([(1, 400_000), (1, 100_000), (0, 0)])
    asset = MediaAsset(
        id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        kind="recipe_image",
        storage_key="images/new.png",
        original_filename="new.png",
        mime_type="image/png",
        byte_size=600_000,
        sha256="a" * 64,
    )
    with pytest.raises(MediaQuotaExceeded, match="Rezept"):
        media_quota.enforce_recipe_quota(
            db,  # type: ignore[arg-type]
            uuid.uuid4(),
            [asset],
            settings=settings,
        )


def test_low_disk_reserve_rejects_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path, storage_min_free_mb=10)
    monkeypatch.setattr(
        storage.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=20_000_000, used=8_000_000, free=12_000_000),
    )
    with pytest.raises(StorageCapacityExceeded):
        storage.ensure_storage_capacity(3 * 1024 * 1024, settings)


@pytest.mark.parametrize(("referenced", "expected_removed"), [(False, 1), (True, 0)])
def test_expired_terminal_source_cleanup_preserves_referenced_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    referenced: bool,
    expected_removed: int,
) -> None:
    settings = settings_for(tmp_path, import_source_retention_hours=24)
    path = tmp_path / "expired.png"
    path.write_bytes(b"expired")
    asset_id = uuid.uuid4()
    job = SimpleNamespace(id=uuid.uuid4(), source_asset_id=asset_id)
    asset = SimpleNamespace(id=asset_id, storage_key="imports/expired.png")

    class CleanupDB:
        def __init__(self) -> None:
            self.deleted: list[Any] = []
            self.commits = 0

        def __enter__(self) -> CleanupDB:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        def scalars(self, _statement: Any) -> list[Any]:
            return [job]

        def scalar(self, _statement: Any) -> uuid.UUID | None:
            return uuid.uuid4() if referenced else None

        def get(self, _model: Any, identifier: uuid.UUID) -> Any:
            return asset if identifier == asset_id else None

        def flush(self) -> None:
            return None

        def delete(self, value: Any) -> None:
            self.deleted.append(value)

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            return None

    db = CleanupDB()
    from app import database

    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(media_quota, "resolve_storage_key", lambda *_args: path)

    assert media_quota.cleanup_terminal_import_sources(settings) == expected_removed
    assert job.source_asset_id == (asset_id if referenced else None)
    assert db.deleted == ([] if referenced else [asset])
    assert db.commits == 1
    assert path.exists() is referenced


def test_unsigned_imported_author_email_never_resolves_local_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = RecipePackage.model_validate(
        {
            "schema_version": "1.1",
            "recipe": {
                "title": "Portable Suppe",
                "base_servings": "4",
                "serving_label": "Personen",
                "comments": [
                    {
                        "author_name": "Unverifizierter Import",
                        "author_email": "%@example.test",
                        "text": "Importierter Text",
                        "created_at": "2026-08-29T12:00:00Z",
                    }
                ],
            },
        }
    )
    recipe = SimpleNamespace(
        id=uuid.uuid4(),
        images=[],
        original_assets=[],
        comments=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        created_by_name_snapshot=None,
        updated_by_name_snapshot=None,
    )

    class ImportDB:
        def __init__(self) -> None:
            self.scalar = Mock(side_effect=AssertionError("local User lookup is forbidden"))
            self.commits = 0

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            return None

    db = ImportDB()
    user = User(
        id=uuid.uuid4(),
        email="importer@example.test",
        display_name="Importer",
        password_hash="not-used",
        role="member",
        is_active=True,
    )
    monkeypatch.setattr(json_import, "create_recipe", lambda *_args: recipe)
    monkeypatch.setattr(json_import, "refresh_search_document", lambda *_args: None)

    imported = json_import.import_recipe_package(db, package, user)  # type: ignore[arg-type]
    assert imported.comments[0].author_user_id is None
    db.scalar.assert_not_called()


def _export_recipe(comment_author: User | None = None) -> SimpleNamespace:
    now = datetime.now(UTC)
    comment = SimpleNamespace(
        author=comment_author,
        author_name_snapshot="Anzeigename",
        text="Kommentar",
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="portable-suppe",
        title="Portable Suppe",
        description=None,
        base_servings=4,
        serving_label="Personen",
        prep_time_minutes=None,
        cook_time_minutes=None,
        rest_time_minutes=None,
        total_time_minutes=None,
        total_time_is_manual=False,
        nutrition=[],
        notes=None,
        status="active",
        ingredient_groups=[],
        instruction_steps=[],
        categories=[],
        tags=[],
        source=None,
        comments=[comment],
        images=[],
        original_assets=[],
        created_at=now,
        updated_at=now,
        created_by_name_snapshot="Importer",
        updated_by_name_snapshot="Importer",
        created_by=None,
        updated_by=None,
    )


def test_member_recipe_export_omits_local_comment_email(tmp_path: Path) -> None:
    local_user = User(
        id=uuid.uuid4(),
        email="private@example.test",
        display_name="Privat",
        password_hash="not-used",
        role="member",
        is_active=True,
    )
    package = exports.recipe_package_dict(
        _export_recipe(local_user),
        settings=settings_for(tmp_path),  # type: ignore[arg-type]
    )
    assert package["recipe"]["comments"][0]["author_email"] is None
    assert "private@example.test" not in str(package)


def test_printable_recipe_images_are_cover_first_and_use_portable_thumbnails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = tmp_path / "first.jpg"
    cover_path = tmp_path / "cover.jpg"
    first_path.write_bytes(b"first printable image")
    cover_path.write_bytes(b"cover printable image")

    first_thumbnail = SimpleNamespace(
        id=uuid.uuid4(), storage_key="derivatives/first.jpg", mime_type="image/jpeg"
    )
    cover_thumbnail = SimpleNamespace(
        id=uuid.uuid4(), storage_key="derivatives/cover.jpg", mime_type="image/jpeg"
    )
    first = SimpleNamespace(
        id=uuid.uuid4(),
        thumbnail_asset=first_thumbnail,
        asset=SimpleNamespace(id=uuid.uuid4()),
        alt_text=None,
        caption=None,
    )
    cover = SimpleNamespace(
        id=uuid.uuid4(),
        thumbnail_asset=cover_thumbnail,
        asset=SimpleNamespace(id=uuid.uuid4()),
        alt_text="Das fertige Gericht",
        caption="Direkt aus dem Ofen",
    )
    recipe = _export_recipe()
    recipe.images = [first, cover]
    recipe.cover_image = cover
    paths = {
        first_thumbnail.storage_key: first_path,
        cover_thumbnail.storage_key: cover_path,
    }
    monkeypatch.setattr(exports, "resolve_storage_key", lambda key: paths[key])

    browser_images = exports.printable_recipe_images(recipe)
    assert [image["src"] for image in browser_images] == [
        f"/api/v1/assets/{cover_thumbnail.id}/view",
        f"/api/v1/assets/{first_thumbnail.id}/view",
    ]
    assert browser_images[0] == {
        "src": f"/api/v1/assets/{cover_thumbnail.id}/view",
        "alt_text": "Das fertige Gericht",
        "caption": "Direkt aus dem Ofen",
        "is_cover": True,
    }
    assert browser_images[1]["alt_text"] == recipe.title

    embedded_images = exports.printable_recipe_images(recipe, embed=True)
    prefix = "data:image/jpeg;base64,"
    assert embedded_images[0]["src"].startswith(prefix)
    assert base64.b64decode(embedded_images[0]["src"].removeprefix(prefix)) == (
        b"cover printable image"
    )
    assert base64.b64decode(embedded_images[1]["src"].removeprefix(prefix)) == (
        b"first printable image"
    )

    html = exports.templates.get_template("recipes/print.html").render(
        recipe=recipe,
        desired_servings=Decimal("4"),
        groups=[],
        print_images=browser_images,
        include_comments=False,
        pdf_mode=True,
        print_css="",
    )
    assert 'class="recipe-images recipe-images--cover"' in html
    assert 'class="recipe-images recipe-images--additional"' in html
    assert html.index(str(cover_thumbnail.id)) < html.index("Weitere Bilder")
    assert html.index("Weitere Bilder") < html.index(str(first_thumbnail.id))
    assert "Direkt aus dem Ofen" in html


def test_export_budget_is_checked_before_first_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path, recipe_json_export_max_mb=1)
    read_bytes = Mock(side_effect=AssertionError("file must not be read"))
    fake_path = SimpleNamespace(
        stat=lambda: SimpleNamespace(st_size=900_000),
        read_bytes=read_bytes,
    )
    asset = SimpleNamespace(
        storage_key="images/large.png",
        byte_size=900_000,
        original_filename="large.png",
        mime_type="image/png",
        sha256="a" * 64,
        kind="recipe_image",
    )
    recipe = _export_recipe()
    recipe.images = [
        SimpleNamespace(
            asset=asset,
            caption=None,
            alt_text=None,
            is_cover=True,
            generation_metadata=None,
        )
    ]
    monkeypatch.setattr(exports, "resolve_storage_key", lambda _key: fake_path)

    with pytest.raises(exports.RecipeExportTooLarge):
        exports.recipe_package_dict(recipe, settings=settings)
    read_bytes.assert_not_called()


def test_export_budget_counts_untrusted_asset_metadata_before_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path, recipe_json_export_max_mb=1)
    read_bytes = Mock(side_effect=AssertionError("file must not be read"))
    fake_path = SimpleNamespace(
        stat=lambda: SimpleNamespace(st_size=1),
        read_bytes=read_bytes,
    )
    asset = SimpleNamespace(
        storage_key="images/small.png",
        byte_size=1,
        original_filename="small.png",
        mime_type="image/png",
        sha256="a" * 64,
        kind="recipe_image",
    )
    recipe = _export_recipe()
    recipe.images = [
        SimpleNamespace(
            asset=asset,
            caption=None,
            alt_text=None,
            is_cover=True,
            generation_metadata={"untrusted": "\u0000" * 200_000},
        )
    ]
    monkeypatch.setattr(exports, "resolve_storage_key", lambda _key: fake_path)

    with pytest.raises(exports.RecipeExportTooLarge):
        exports.recipe_package_dict(recipe, settings=settings)
    read_bytes.assert_not_called()


def test_export_api_returns_413_and_concurrency_gate_is_nonblocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _export_recipe()
    monkeypatch.setattr(exports_api, "get_recipe", lambda *_args: recipe)
    monkeypatch.setattr(
        exports_api,
        "recipe_package_dict",
        Mock(side_effect=exports.RecipeExportTooLarge("zu groß")),
    )
    with pytest.raises(HTTPException) as too_large:
        exports_api.export_json(
            recipe.id,
            include_originals=True,
            _=User(id=uuid.uuid4()),  # type: ignore[call-arg]
            db=SimpleNamespace(),  # type: ignore[arg-type]
        )
    assert too_large.value.status_code == 413

    semaphore = threading.BoundedSemaphore(1)
    semaphore.acquire()
    monkeypatch.setattr(exports, "json_export_slots", semaphore)
    with pytest.raises(exports.RecipeExportBusy), exports.json_export_slot():
        pass
    semaphore.release()
