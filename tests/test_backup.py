from __future__ import annotations

import hashlib
import json
import stat
import uuid
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app import __version__
from app.backups.exporter import BACKUP_MODELS, encode_value
from app.backups.migrations import normalize_tables
from app.backups.preflight import (
    DATABASE_SCHEMA_VERSION,
    InvalidBackup,
    _parse_checksums,
    _validate_category_graph,
    preflight_backup,
    safe_archive_name,
)
from app.backups.restorer import (
    PurePathWithoutTraversal,
    _extract_media,
    _parent_first_categories,
    decode_value,
    restore_backup,
)
from app.backups.schemas import BackupManifest
from app.config import Settings

ArchiveMutator = Callable[[dict[str, bytes]], None]


def _typed_uuid(value: uuid.UUID | str) -> dict[str, str]:
    return {"$type": "uuid", "value": str(value)}


def _empty_tables() -> dict[str, list[dict[str, Any]]]:
    tables = {model.__table__.name: [] for model in BACKUP_MODELS}
    tables["users"] = [
        {
            "id": _typed_uuid(uuid.UUID("11111111-1111-4111-8111-111111111111")),
            "email": "admin@example.test",
            "password_hash": "not-used-by-preflight",
            "role": "admin",
            "is_active": True,
            "language": None,
        }
    ]
    return tables


def _schema_0001_tables() -> dict[str, list[dict[str, Any]]]:
    tables = _empty_tables()
    for name in (
        "tags",
        "search_synonyms",
        "recipe_tags",
        "recipe_shares",
        "recipe_nutrition",
        "import_candidates",
        "user_notes",
    ):
        tables.pop(name)
    tables["shopping_list_items"] = [
        {
            "id": _typed_uuid(uuid.UUID("22222222-2222-4222-8222-222222222222")),
            "user_id": tables["users"][0]["id"],
            "recipe_id": None,
            "text": "Servietten",
            "amount": "2 Packungen",
            "checked": False,
            "position": 0,
        }
    ]
    tables["meal_plan_entries"] = []
    return tables


def _schema_0002_tables() -> dict[str, list[dict[str, Any]]]:
    tables = _empty_tables()
    tables.pop("recipe_nutrition")
    tables.pop("import_candidates")
    tables.pop("user_notes")
    tables["shopping_list_items"] = [
        {
            "id": _typed_uuid(uuid.UUID("22222222-2222-4222-8222-222222222222")),
            "user_id": tables["users"][0]["id"],
            "recipe_id": None,
            "text": "Servietten",
            "amount": "2 Packungen",
            "checked": False,
            "position": 0,
            "unit": None,
            "amount_min": None,
            "amount_max": None,
            "normalized_key": None,
            "is_manual": True,
            "source_recipe_ids": [],
        }
    ]
    tables["meal_plan_entries"] = []
    return tables


def _schema_0004_tables() -> dict[str, list[dict[str, Any]]]:
    tables = _empty_tables()
    tables.pop("recipe_nutrition")
    tables.pop("import_candidates")
    tables.pop("user_notes")
    return tables


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        storage_root=tmp_path / "storage",
        backup_temp_root=tmp_path / "backup-temp",
        max_backup_upload_mb=1,
    )


def _write_backup(
    path: Path,
    *,
    tables: dict[str, list[dict[str, Any]]] | None = None,
    media: Mapping[str, bytes] | None = None,
    application_format: str = "rezeptverwaltung-application-data",
    application_version: str = "1.0",
    application_mutator: ArchiveMutator | None = None,
    manifest_overrides: Mapping[str, Any] | None = None,
    checksum_lines: list[str] | None = None,
    extra_entries: Mapping[str, bytes | zipfile.ZipInfo] | None = None,
    add_media_rows: bool = True,
) -> Path:
    tables = tables if tables is not None else _empty_tables()
    media = media or {}

    if add_media_rows:
        media_rows = tables["media_assets"]
        known_storage_keys = {row.get("storage_key") for row in media_rows}
        for index, (storage_key, payload) in enumerate(media.items()):
            if storage_key in known_storage_keys:
                continue
            media_rows.append(
                {
                    "id": _typed_uuid(uuid.UUID(int=index + 100)),
                    "kind": "recipe_image",
                    "storage_key": storage_key,
                    "original_filename": Path(storage_key).name,
                    "mime_type": "application/octet-stream",
                    "byte_size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )

    application = {
        "format": application_format,
        "version": application_version,
        "tables": tables,
    }
    application_bytes = json.dumps(application, separators=(",", ":")).encode()
    counts = {name: len(rows) for name, rows in tables.items()}
    manifest_data: dict[str, Any] = {
        "application_version": __version__,
        "database_schema_version": DATABASE_SCHEMA_VERSION,
        "created_at": datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
        "counts": counts,
        "media_file_count": len(media),
        "media_total_bytes": sum(len(payload) for payload in media.values()),
        "archive_contents": [
            "manifest.json",
            "application-data.json",
            "checksums.sha256",
            "media/",
        ],
    }
    manifest_data.update(manifest_overrides or {})
    manifest_bytes = BackupManifest.model_validate(manifest_data).model_dump_json().encode()

    files = {
        "manifest.json": manifest_bytes,
        "application-data.json": application_bytes,
        **{f"media/{name}": payload for name, payload in media.items()},
    }
    if application_mutator is not None:
        application_mutator(files)

    if checksum_lines is None:
        checksum_lines = [
            f"{hashlib.sha256(payload).hexdigest()}  {name}"
            for name, payload in sorted(files.items())
        ]
    checksum_bytes = ("\n".join(checksum_lines) + "\n").encode()

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("checksums.sha256", checksum_bytes)
        for name, value in (extra_entries or {}).items():
            if isinstance(value, zipfile.ZipInfo):
                archive.writestr(value, b"target")
            else:
                archive.writestr(name, value)
    return path


def _checksum_lines_for(files: Mapping[str, bytes]) -> list[str]:
    return [
        f"{hashlib.sha256(payload).hexdigest()}  {name}" for name, payload in sorted(files.items())
    ]


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "../escape",
        "media/../../escape",
        "/absolute/path",
        "C:\\Windows\\system.ini",
        "media/image\x00.png",
    ],
)
def test_safe_archive_name_rejects_unsafe_paths(name: str) -> None:
    with pytest.raises(InvalidBackup, match="Dateipfad"):
        safe_archive_name(name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("manifest.json", "manifest.json"),
        ("media/2026/08/image.png", "media/2026/08/image.png"),
        ("übung/gericht.jpg", "übung/gericht.jpg"),
    ],
)
def test_safe_archive_name_accepts_normal_relative_posix_paths(name: str, expected: str) -> None:
    assert safe_archive_name(name) == expected


def test_preflight_accepts_minimal_valid_backup(tmp_path: Path) -> None:
    archive = _write_backup(tmp_path / "valid.zip")

    result = preflight_backup(archive, _settings(tmp_path))

    assert result.valid is True
    assert result.application_version == __version__
    assert result.backup_format_version == "1.0"
    assert result.counts["users"] == 1
    assert result.media_file_count == 0
    assert result.warnings == []
    assert result.required_disk_bytes > 0


def test_preflight_warns_for_a_different_application_version(tmp_path: Path) -> None:
    archive = _write_backup(
        tmp_path / "other-version.zip",
        manifest_overrides={"application_version": "0.8.0"},
    )

    result = preflight_backup(archive, _settings(tmp_path))

    assert result.valid is True
    assert len(result.warnings) == 1
    assert "anderen Anwendungsversion" in result.warnings[0]


def test_preflight_migrates_a_real_schema_0001_archive_in_memory(tmp_path: Path) -> None:
    media = {"2026/08/dish.bin": b"unchanged-media"}
    archive = _write_backup(
        tmp_path / "schema-0001.zip",
        tables=_schema_0001_tables(),
        media=media,
        manifest_overrides={"database_schema_version": "0001"},
    )

    result = preflight_backup(archive, _settings(tmp_path))

    expected_tables = {model.__table__.name for model in BACKUP_MODELS}
    assert result.source_database_schema_version == "0001"
    assert set(result.counts) == expected_tables
    assert set(result.normalized_tables) == expected_tables
    assert list(result.normalized_tables) == [model.__table__.name for model in BACKUP_MODELS]
    assert result.counts["tags"] == 0
    assert result.counts["search_synonyms"] == 0
    assert result.counts["recipe_tags"] == 0
    assert result.counts["recipe_shares"] == 0
    assert "shopping_list_items" not in result.normalized_tables
    assert "meal_plan_entries" not in result.normalized_tables
    assert result.media_checksums == {
        "media/2026/08/dish.bin": hashlib.sha256(media["2026/08/dish.bin"]).hexdigest()
    }
    assert any(
        "0001" in warning and DATABASE_SCHEMA_VERSION in warning for warning in result.warnings
    )
    assert "normalized_tables" not in result.model_dump()
    assert "media_checksums" not in result.model_dump()


@pytest.mark.parametrize("schema_version", ["0002", "0003"])
def test_preflight_discards_removed_tables_from_legacy_backups(
    tmp_path: Path, schema_version: str
) -> None:
    archive = _write_backup(
        tmp_path / f"schema-{schema_version}.zip",
        tables=_schema_0002_tables(),
        manifest_overrides={"database_schema_version": schema_version},
    )

    result = preflight_backup(archive, _settings(tmp_path))

    assert result.source_database_schema_version == schema_version
    assert result.normalized_tables == _empty_tables()
    assert any(
        schema_version in warning and DATABASE_SCHEMA_VERSION in warning
        for warning in result.warnings
    )


def test_preflight_adds_empty_nutrition_table_for_schema_0004(tmp_path: Path) -> None:
    archive = _write_backup(
        tmp_path / "schema-0004.zip",
        tables=_schema_0004_tables(),
        manifest_overrides={"database_schema_version": "0004"},
    )

    result = preflight_backup(archive, _settings(tmp_path))

    assert result.source_database_schema_version == "0004"
    assert result.normalized_tables["recipe_nutrition"] == []


def test_preflight_adds_empty_user_notes_table_for_schema_0009(tmp_path: Path) -> None:
    tables = _empty_tables()
    tables.pop("user_notes")
    archive = _write_backup(
        tmp_path / "schema-0009.zip",
        tables=tables,
        manifest_overrides={"database_schema_version": "0009"},
    )

    result = preflight_backup(archive, _settings(tmp_path))

    assert result.source_database_schema_version == "0009"
    assert result.normalized_tables["user_notes"] == []


def test_schema_0011_derives_recipe_kinds_from_the_baking_category_tree() -> None:
    tables = _empty_tables()
    cooking_id = uuid.UUID("22222222-2222-4222-8222-222222222221")
    baking_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    baking_root_id = uuid.UUID("33333333-3333-4333-8333-333333333331")
    cake_id = uuid.UUID("33333333-3333-4333-8333-333333333332")
    timestamp = encode_value(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))
    tables["recipes"] = [
        {
            "id": _typed_uuid(cooking_id),
            "status": "active",
            "search_document": "Suppe",
            "search_vector": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        },
        {
            "id": _typed_uuid(baking_id),
            "status": "active",
            "search_document": "Kuchen",
            "search_vector": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    ]
    tables["categories"] = [
        {
            "id": _typed_uuid(baking_root_id),
            "parent_id": None,
            "name": "Backen",
            "normalized_name": "backen",
            "slug": "backen",
            "position": 0,
        },
        {
            "id": _typed_uuid(cake_id),
            "parent_id": _typed_uuid(baking_root_id),
            "name": "Kuchen",
            "normalized_name": "kuchen",
            "slug": "kuchen",
            "position": 0,
        },
    ]
    tables["recipe_categories"] = [
        {"recipe_id": _typed_uuid(baking_id), "category_id": _typed_uuid(cake_id)}
    ]
    tables["recipe_versions"] = [
        {
            "recipe_id": _typed_uuid(baking_id),
            "version_number": 1,
            "snapshot": {"title": "Kuchen"},
        }
    ]
    tables["import_candidates"] = [
        {
            "recipe_payload": {
                "title": "Brot",
                "categories": [{"path": ["Backen", "Brot"]}],
            }
        }
    ]

    normalized = normalize_tables("0011", tables)

    assert [row["recipe_kind"] for row in normalized["recipes"]] == [
        "cooking",
        "baking",
    ]
    assert normalized["recipe_versions"][0]["snapshot"]["recipe_kind"] == "baking"
    assert normalized["import_candidates"][0]["recipe_payload"]["recipe_kind"] == "baking"


def test_schema_0005_drafts_and_review_categories_are_activated_deterministically() -> None:
    tables = _empty_tables()
    tables.pop("import_candidates")
    tables.pop("user_notes")
    recipe_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    job_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    timestamp = encode_value(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))
    tables["recipes"] = [
        {
            "id": _typed_uuid(recipe_id),
            "status": "draft",
            "search_document": "Kartoffelsuppe",
            "search_vector": "'kartoffelsupp':1A",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    ]
    tables["import_jobs"] = [
        {
            "id": _typed_uuid(job_id),
            "status": "review",
            "result_recipe_id": _typed_uuid(recipe_id),
            "current_stage": "Zur Prüfung bereit",
            "suggestions_json": {
                "categories": [
                    {"path": ["Alltag", "Suppen"], "origin": "ai_import"},
                    {"path": ["alltag", "suppEN"], "origin": "ai_import"},
                ],
                "confidence": "high",
            },
        }
    ]

    first = normalize_tables("0005", tables)
    second = normalize_tables("0005", tables)

    assert first == second
    assert first["recipes"][0]["status"] == "active"
    assert first["recipes"][0]["search_vector"] is None
    assert "Alltag › Suppen" in first["recipes"][0]["search_document"]
    assert [category["name"] for category in first["categories"]] == ["Alltag", "Suppen"]
    assert len(first["recipe_categories"]) == 1
    assert first["import_jobs"][0]["status"] == "completed"
    assert first["import_jobs"][0]["current_stage"] == "Import abgeschlossen"
    assert first["import_jobs"][0]["suggestions_json"] == {"confidence": "high"}


def test_schema_0001_manifest_counts_are_checked_before_migration(tmp_path: Path) -> None:
    tables = _schema_0001_tables()
    counts = {name: len(rows) for name, rows in tables.items()}
    counts["users"] += 1
    archive = _write_backup(
        tmp_path / "schema-0001-wrong-count.zip",
        tables=tables,
        manifest_overrides={"database_schema_version": "0001", "counts": counts},
    )

    with pytest.raises(InvalidBackup, match="Tabellenzählungen"):
        preflight_backup(archive, _settings(tmp_path))


def test_removed_schema_0001_rows_are_not_restored_or_relationally_validated(
    tmp_path: Path,
) -> None:
    tables = _schema_0001_tables()
    tables["shopping_list_items"][0].pop("user_id")
    archive = _write_backup(
        tmp_path / "schema-0001-invalid-relation.zip",
        tables=tables,
        manifest_overrides={"database_schema_version": "0001"},
    )

    result = preflight_backup(archive, _settings(tmp_path))

    assert result.valid is True
    assert "shopping_list_items" not in result.normalized_tables
    assert "meal_plan_entries" not in result.normalized_tables


def test_preflight_rejects_non_zip_input(tmp_path: Path) -> None:
    archive = tmp_path / "not-a-zip.zip"
    archive.write_bytes(b"this is not a zip file")

    with pytest.raises(InvalidBackup, match="kein gültiges ZIP-Backup"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_missing_and_oversized_archive(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(InvalidBackup, match="fehlt oder überschreitet"):
        preflight_backup(tmp_path / "missing.zip", settings)

    oversized = tmp_path / "oversized.zip"
    oversized.write_bytes(b"x" * (settings.max_backup_upload_bytes + 1))
    with pytest.raises(InvalidBackup, match="fehlt oder überschreitet"):
        preflight_backup(oversized, settings)


def test_preflight_requires_all_contract_files(tmp_path: Path) -> None:
    archive_path = tmp_path / "missing-required.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", b"{}")

    with pytest.raises(InvalidBackup, match="fehlen erforderliche Dateien"):
        preflight_backup(archive_path, _settings(tmp_path))


@pytest.mark.parametrize(
    "unsafe_name",
    ["../outside", "media/../../outside", "/absolute", "media\\outside"],
)
def test_preflight_rejects_zip_slip_entries(tmp_path: Path, unsafe_name: str) -> None:
    archive = _write_backup(
        tmp_path / "zip-slip.zip",
        extra_entries={unsafe_name: b"malicious"},
    )

    with pytest.raises(InvalidBackup, match="Dateipfad"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_case_insensitive_filename_collisions(tmp_path: Path) -> None:
    archive = _write_backup(
        tmp_path / "collision.zip",
        extra_entries={"Manifest.JSON": b"shadow"},
    )

    with pytest.raises(InvalidBackup, match="kollidierende Dateinamen"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_symlinks(tmp_path: Path) -> None:
    symlink = zipfile.ZipInfo("media/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = _write_backup(
        tmp_path / "symlink.zip",
        extra_entries={"media/link": symlink},
    )

    with pytest.raises(InvalidBackup, match="unzulässige Dateiverweise"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_unexpected_regular_files(tmp_path: Path) -> None:
    payload = b"not part of the backup contract"
    archive = _write_backup(
        tmp_path / "unexpected.zip",
        extra_entries={"notes.txt": payload},
    )

    with pytest.raises(InvalidBackup, match="unerwartete Dateien"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_suspicious_compression_ratio(tmp_path: Path) -> None:
    archive = _write_backup(
        tmp_path / "compression-bomb.zip",
        extra_entries={"media/highly-compressible.bin": b"0" * 100_000},
    )

    with pytest.raises(InvalidBackup, match="verdächtig stark komprimierte Daten"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_checksum_mismatch(tmp_path: Path) -> None:
    def replace_application_after_checksums(files: dict[str, bytes]) -> None:
        files["application-data.json"] = b'{"tampered":true}'

    original_tables = _empty_tables()
    application = {
        "format": "rezeptverwaltung-application-data",
        "version": "1.0",
        "tables": original_tables,
    }
    original_application = json.dumps(application, separators=(",", ":")).encode()
    manifest = (
        BackupManifest(
            application_version=__version__,
            database_schema_version=DATABASE_SCHEMA_VERSION,
            created_at=datetime(2026, 8, 29, tzinfo=UTC),
            counts={name: len(rows) for name, rows in original_tables.items()},
            media_file_count=0,
            media_total_bytes=0,
        )
        .model_dump_json()
        .encode()
    )
    checksum_lines = _checksum_lines_for(
        {"manifest.json": manifest, "application-data.json": original_application}
    )
    archive = _write_backup(
        tmp_path / "tampered.zip",
        tables=original_tables,
        application_mutator=replace_application_after_checksums,
        checksum_lines=checksum_lines,
    )

    with pytest.raises(InvalidBackup, match="Prüfsumme von application-data.json"):
        preflight_backup(archive, _settings(tmp_path))


def test_checksum_parser_rejects_malformed_duplicate_and_unsafe_entries() -> None:
    valid_digest = "a" * 64

    with pytest.raises(InvalidBackup, match="Prüfsummenliste ist ungültig"):
        _parse_checksums(f"{valid_digest} manifest.json\n".encode())
    with pytest.raises(InvalidBackup, match="Prüfsummenliste ist ungültig"):
        _parse_checksums(f"{'A' * 64}  manifest.json\n".encode())
    with pytest.raises(InvalidBackup, match="doppelte Einträge"):
        _parse_checksums(f"{valid_digest}  manifest.json\n{valid_digest}  manifest.json\n".encode())
    with pytest.raises(InvalidBackup, match="Dateipfad"):
        _parse_checksums(f"{valid_digest}  ../manifest.json\n".encode())


def test_preflight_rejects_incomplete_checksum_inventory(tmp_path: Path) -> None:
    archive = _write_backup(
        tmp_path / "missing-checksum.zip",
        checksum_lines=[f"{'0' * 64}  manifest.json"],
    )

    with pytest.raises(InvalidBackup, match="unvollständig oder enthält Zusätze"):
        preflight_backup(archive, _settings(tmp_path))


@pytest.mark.parametrize("schema_version", ["0000", "0013", "9999", "custom"])
def test_preflight_rejects_unsupported_database_schema(tmp_path: Path, schema_version: str) -> None:
    archive = _write_backup(
        tmp_path / f"unsupported-schema-{schema_version}.zip",
        manifest_overrides={"database_schema_version": schema_version},
    )

    with pytest.raises(InvalidBackup, match="Datenbankschemaversion"):
        preflight_backup(archive, _settings(tmp_path))


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        ("manifest.json", b"{not-json"),
        ("manifest.json", b"{}"),
        ("application-data.json", b"{not-json"),
        ("application-data.json", b"[]"),
    ],
)
def test_preflight_translates_invalid_metadata_to_domain_error(
    tmp_path: Path,
    target: str,
    payload: bytes,
) -> None:
    def corrupt_metadata(files: dict[str, bytes]) -> None:
        files[target] = payload

    archive = _write_backup(
        tmp_path / f"invalid-{target.replace('.', '-')}.zip",
        application_mutator=corrupt_metadata,
    )

    with pytest.raises(InvalidBackup, match="ungültig"):
        preflight_backup(archive, _settings(tmp_path))


@pytest.mark.parametrize(
    ("application_format", "application_version", "message"),
    [
        ("another-format", "1.0", "Anwendungsdatenformat"),
        ("rezeptverwaltung-application-data", "2.0", "Anwendungsdatenversion"),
    ],
)
def test_preflight_rejects_unsupported_application_contract(
    tmp_path: Path,
    application_format: str,
    application_version: str,
    message: str,
) -> None:
    archive = _write_backup(
        tmp_path / "unsupported-application.zip",
        application_format=application_format,
        application_version=application_version,
    )

    with pytest.raises(InvalidBackup, match=message):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_requires_exact_table_and_manifest_count_sets(tmp_path: Path) -> None:
    tables = _empty_tables()
    tables.pop("audit_logs")
    archive = _write_backup(tmp_path / "missing-table.zip", tables=tables)

    with pytest.raises(InvalidBackup, match="Tabellenliste"):
        preflight_backup(archive, _settings(tmp_path))

    complete_tables = _empty_tables()
    counts = {name: len(rows) for name, rows in complete_tables.items()}
    counts.pop("audit_logs")
    archive = _write_backup(
        tmp_path / "missing-count.zip",
        tables=complete_tables,
        manifest_overrides={"counts": counts},
    )

    with pytest.raises(InvalidBackup, match="Tabellenliste"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_incorrect_row_count(tmp_path: Path) -> None:
    tables = _empty_tables()
    counts = {name: len(rows) for name, rows in tables.items()}
    counts["users"] = 2
    archive = _write_backup(
        tmp_path / "wrong-row-count.zip",
        tables=tables,
        manifest_overrides={"counts": counts},
    )

    with pytest.raises(InvalidBackup, match="Tabellenzählungen"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_requires_an_active_admin(tmp_path: Path) -> None:
    tables = _empty_tables()
    tables["users"][0]["is_active"] = False
    archive = _write_backup(tmp_path / "no-active-admin.zip", tables=tables)

    with pytest.raises(InvalidBackup, match="keinen aktiven Administrator"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_accepts_and_reconciles_media(tmp_path: Path) -> None:
    payload = b"real recipe image bytes"
    archive = _write_backup(
        tmp_path / "with-media.zip",
        media={"2026/08/dish.bin": payload},
    )

    result = preflight_backup(archive, _settings(tmp_path))

    assert result.valid is True
    assert result.media_file_count == 1
    assert result.media_total_bytes == len(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sha256", "0" * 64, "Datenbank-Prüfsumme"),
        ("byte_size", 999, "Mediendateigröße"),
    ],
)
def test_preflight_rejects_media_metadata_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = b"media"
    tables = _empty_tables()
    tables["media_assets"] = [
        {
            "storage_key": "dish.bin",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
        }
    ]
    tables["media_assets"][0][field] = value
    archive = _write_backup(
        tmp_path / f"bad-{field}.zip",
        tables=tables,
        media={"dish.bin": payload},
        add_media_rows=False,
    )

    with pytest.raises(InvalidBackup, match=message):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_unreferenced_media_file(tmp_path: Path) -> None:
    tables = _empty_tables()
    archive = _write_backup(
        tmp_path / "unreferenced-media.zip",
        tables=tables,
        media={"orphan.bin": b"orphan"},
        manifest_overrides={"counts": {name: len(rows) for name, rows in tables.items()}},
        add_media_rows=False,
    )

    with pytest.raises(InvalidBackup, match="Datenbankreferenzen"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_duplicate_media_storage_keys(tmp_path: Path) -> None:
    payload = b"media"
    digest = hashlib.sha256(payload).hexdigest()
    tables = _empty_tables()
    tables["media_assets"] = [
        {"storage_key": "dish.bin", "sha256": digest, "byte_size": len(payload)},
        {"storage_key": "dish.bin", "sha256": digest, "byte_size": len(payload)},
    ]
    archive = _write_backup(
        tmp_path / "duplicate-storage-key.zip",
        tables=tables,
        media={"dish.bin": payload},
        add_media_rows=False,
    )

    with pytest.raises(InvalidBackup, match="doppelte Speicherschlüssel"):
        preflight_backup(archive, _settings(tmp_path))


def test_category_graph_accepts_parent_child_tree_in_any_row_order() -> None:
    parent = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    child = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    rows = [
        {"id": _typed_uuid(child), "parent_id": _typed_uuid(parent)},
        {"id": _typed_uuid(parent), "parent_id": None},
    ]

    _validate_category_graph(rows)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {
                    "id": _typed_uuid("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                    "parent_id": _typed_uuid("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                }
            ],
            "fehlendes Elternelement",
        ),
        (
            [
                {
                    "id": _typed_uuid("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                    "parent_id": _typed_uuid("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                },
                {
                    "id": _typed_uuid("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                    "parent_id": _typed_uuid("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                },
            ],
            "Zyklus",
        ),
        (
            [
                {"id": _typed_uuid("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), "parent_id": None},
                {"id": _typed_uuid("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), "parent_id": None},
            ],
            "doppelte Kategorien",
        ),
        ([{"id": "not-a-typed-uuid", "parent_id": None}], "ungültige UUID"),
        (
            [{"id": {"$type": "uuid", "value": "definitely-not-a-uuid"}, "parent_id": None}],
            "ungültige UUID",
        ),
        (["not-an-object"], "Kategoriedaten"),
    ],
)
def test_category_graph_rejects_invalid_structures(rows: list[object], message: str) -> None:
    with pytest.raises(InvalidBackup, match=message):
        _validate_category_graph(rows)


def test_preflight_runs_category_graph_validation(tmp_path: Path) -> None:
    identifier = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    tables = _empty_tables()
    tables["categories"] = [{"id": _typed_uuid(identifier), "parent_id": _typed_uuid(identifier)}]
    archive = _write_backup(tmp_path / "category-cycle.zip", tables=tables)

    with pytest.raises(InvalidBackup, match="Zyklus"):
        preflight_backup(archive, _settings(tmp_path))


def test_preflight_rejects_malformed_typed_category_uuid(tmp_path: Path) -> None:
    tables = _empty_tables()
    tables["categories"] = [
        {"id": {"$type": "uuid", "value": "not-a-real-uuid"}, "parent_id": None}
    ]
    archive = _write_backup(tmp_path / "invalid-category-uuid.zip", tables=tables)

    with pytest.raises(InvalidBackup, match="ungültige UUID"):
        preflight_backup(archive, _settings(tmp_path))


def test_encode_decode_value_round_trip_for_nested_typed_values() -> None:
    identifier = uuid.UUID("12345678-1234-4234-8234-123456789abc")
    timestamp = datetime(2026, 8, 29, 12, 34, 56, tzinfo=UTC)
    original = {
        "id": identifier,
        "created_at": timestamp,
        "amount": Decimal("12.3400"),
        "nested": [identifier, {"amount": Decimal("0.125")}],
        "plain": True,
    }

    assert decode_value(encode_value(original)) == original


def test_parent_first_category_restore_order_is_stable() -> None:
    root = uuid.UUID("10000000-0000-4000-8000-000000000000")
    first = uuid.UUID("20000000-0000-4000-8000-000000000000")
    second = uuid.UUID("30000000-0000-4000-8000-000000000000")
    grandchild = uuid.UUID("40000000-0000-4000-8000-000000000000")
    rows = [
        {"id": grandchild, "parent_id": second, "name": "Enkel", "position": 0},
        {"id": second, "parent_id": root, "name": "Beta", "position": 1},
        {"id": first, "parent_id": root, "name": "Alpha", "position": 1},
        {"id": root, "parent_id": None, "name": "Wurzel", "position": 0},
    ]

    ordered = _parent_first_categories(rows)

    assert [row["id"] for row in ordered] == [root, first, second, grandchild]


def test_parent_first_category_restore_rejects_cycles_and_missing_parents() -> None:
    first = uuid.UUID("10000000-0000-4000-8000-000000000000")
    second = uuid.UUID("20000000-0000-4000-8000-000000000000")

    with pytest.raises(InvalidBackup, match="Zyklus oder fehlende Eltern"):
        _parent_first_categories(
            [
                {"id": first, "parent_id": second},
                {"id": second, "parent_id": first},
            ]
        )
    with pytest.raises(InvalidBackup, match="Zyklus oder fehlende Eltern"):
        _parent_first_categories([{"id": first, "parent_id": second}])


def test_pure_path_without_traversal_returns_relative_platform_path() -> None:
    assert PurePathWithoutTraversal("2026/08/dish.png") == Path("2026", "08", "dish.png")

    with pytest.raises(InvalidBackup, match="Dateipfad"):
        PurePathWithoutTraversal("../../dish.png")


def test_extract_media_writes_nested_files_and_verifies_checksum(tmp_path: Path) -> None:
    payload = b"verified image bytes"
    name = "media/2026/08/dish.bin"
    digest = hashlib.sha256(payload).hexdigest()
    archive_path = tmp_path / "media-only.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("checksums.sha256", f"{digest}  {name}\n")
        archive.writestr(name, payload)

    generation = tmp_path / "generation"
    _extract_media(archive_path, generation, {name: digest})

    assert (generation / "2026" / "08" / "dish.bin").read_bytes() == payload


def test_extract_media_rejects_mismatch_after_writing(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad-media.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("checksums.sha256", f"{'0' * 64}  media/dish.bin\n")
        archive.writestr("media/dish.bin", b"tampered")

    with pytest.raises(InvalidBackup, match="nach dem Entpacken"):
        _extract_media(
            archive_path,
            tmp_path / "generation",
            {"media/dish.bin": "0" * 64},
        )


def test_extract_media_rejects_inventory_changed_after_preflight(tmp_path: Path) -> None:
    archive_path = tmp_path / "changed-media-inventory.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("media/unexpected.bin", b"unexpected")

    with pytest.raises(InvalidBackup, match="nach der Vorabprüfung verändert"):
        _extract_media(archive_path, tmp_path / "generation", {})


def test_restore_consumes_the_schema_0001_preflight_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.backups import restorer

    archive_path = _write_backup(
        tmp_path / "restore-schema-0001.zip",
        tables=_schema_0001_tables(),
        manifest_overrides={"database_schema_version": "0001"},
    )
    settings = _settings(tmp_path)
    settings.ensure_directories()
    generations = settings.storage_root / "generations"
    generations.mkdir(parents=True)
    old_generation = generations / "old"
    old_generation.mkdir()
    restored_admin = SimpleNamespace(id=uuid.UUID("11111111-1111-4111-8111-111111111111"))

    class RecordingDatabase:
        def __init__(self) -> None:
            self.inserts: dict[str, list[dict[str, Any]]] = {}

        def execute(self, statement: object, parameters: object = None) -> None:
            table = getattr(statement, "table", None)
            if table is not None and isinstance(parameters, list):
                self.inserts[table.name] = parameters

        def flush(self) -> None:
            return None

        def merge(self, _value: object) -> None:
            return None

        def add(self, _value: object) -> None:
            return None

        def scalar(self, _statement: object) -> object:
            return restored_admin

        def get_bind(self) -> object:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        def rollback(self) -> None:
            return None

        def commit(self) -> None:
            return None

    database = RecordingDatabase()
    monkeypatch.setattr(
        restorer,
        "export_backup",
        lambda *_args, **_kwargs: (Path("safety"), None, "checksum"),
    )
    monkeypatch.setattr(restorer, "active_storage_root", lambda *_args: old_generation)
    swaps: list[Path] = []
    monkeypatch.setattr(
        restorer,
        "swap_active_generation",
        lambda target, **_kwargs: swaps.append(target),
    )

    result = restore_backup(
        database,  # type: ignore[arg-type]
        archive_path,
        restore_id="33333333-3333-4333-8333-333333333333",
        settings=settings,
    )

    assert result["status"] == "completed"
    assert swaps == [generations / "restore-33333333-3333-4333-8333-333333333333"]
    assert "shopping_list_items" not in database.inserts
    assert "meal_plan_entries" not in database.inserts
    assert "tags" not in database.inserts
    assert "search_synonyms" not in database.inserts
    summary = result["summary"]
    assert isinstance(summary, dict)
    assert summary["source_database_schema_version"] == "0001"


def test_restore_never_reverts_or_deletes_new_generation_after_ambiguous_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.backups import restorer

    settings = _settings(tmp_path)
    settings.ensure_directories()
    generations = settings.storage_root / "generations"
    generations.mkdir(parents=True)
    old_generation = generations / "old"
    old_generation.mkdir()
    restore_id = str(uuid.uuid4())
    archive_path = tmp_path / "restore.zip"
    archive_path.write_bytes(b"placeholder")
    admin = SimpleNamespace(id=uuid.uuid4())

    class AmbiguousDatabase:
        def execute(self, _statement: object, _parameters: object = None) -> None:
            return None

        def flush(self) -> None:
            return None

        def merge(self, _value: object) -> None:
            return None

        def add(self, _value: object) -> None:
            return None

        def scalar(self, _statement: object) -> object:
            return admin

        def get_bind(self) -> object:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        def rollback(self) -> None:
            return None

        def commit(self) -> None:
            raise ConnectionError("commit acknowledgement lost")

    preflight = SimpleNamespace(
        required_disk_bytes=0,
        normalized_tables={model.__table__.name: [] for model in BACKUP_MODELS},
        media_checksums={},
        model_dump=lambda **_kwargs: {"valid": True},
    )
    monkeypatch.setattr(restorer, "preflight_backup", lambda *_args: preflight)
    monkeypatch.setattr(
        restorer, "export_backup", lambda *_args, **_kwargs: (Path("safety"), None, "x")
    )
    monkeypatch.setattr(restorer, "active_storage_root", lambda *_args: old_generation)
    monkeypatch.setattr(
        restorer,
        "_extract_media",
        lambda _archive, generation, _checksums: generation.mkdir(parents=True),
    )
    swaps: list[Path] = []
    monkeypatch.setattr(
        restorer,
        "swap_active_generation",
        lambda target, **_kwargs: swaps.append(target),
    )
    recovered = Mock(return_value=False)
    monkeypatch.setattr(restorer, "recover_interrupted_restore", recovered)

    with pytest.raises(ConnectionError, match="acknowledgement lost"):
        restore_backup(
            AmbiguousDatabase(),  # type: ignore[arg-type]
            archive_path,
            restore_id=restore_id,
            settings=settings,
        )

    new_generation = generations / f"restore-{restore_id}"
    assert swaps == [new_generation]
    assert new_generation.is_dir()
    assert (settings.storage_root / ".restore-journal.json").exists()
    recovered.assert_called_once_with(settings)
