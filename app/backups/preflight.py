from __future__ import annotations

import hashlib
import json
import stat
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import Table, UniqueConstraint

from app import __version__
from app.backups.errors import InvalidBackup as InvalidBackup
from app.backups.exporter import BACKUP_MODELS, table_name
from app.backups.migrations import (
    SUPPORTED_DATABASE_SCHEMA_VERSIONS,
    expected_tables_for_schema,
    normalize_tables,
)
from app.backups.schemas import DATABASE_SCHEMA_VERSION, BackupManifest, PreflightResult
from app.config import Settings, get_settings

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CHECKSUM_BYTES = 32 * 1024 * 1024
MAX_APPLICATION_DATA_BYTES = 512 * 1024 * 1024


def safe_archive_name(name: str) -> str:
    if not name or name == "." or "\x00" in name or "\\" in name:
        raise InvalidBackup("Das Archiv enthält einen ungültigen Dateipfad")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidBackup("Das Archiv enthält einen unsicheren Dateipfad")
    return path.as_posix()


def _parse_checksums(raw: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line:
            continue
        try:
            digest, name = line.split("  ", 1)
        except ValueError as exc:
            raise InvalidBackup("Die Prüfsummenliste ist ungültig") from exc
        name = safe_archive_name(name)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise InvalidBackup("Die Prüfsummenliste ist ungültig")
        if name in result:
            raise InvalidBackup("Die Prüfsummenliste enthält doppelte Einträge")
        result[name] = digest
    return result


def _typed_uuid(value: object) -> str | None:
    if value is None:
        return None
    if (
        isinstance(value, dict)
        and value.get("$type") == "uuid"
        and isinstance(value.get("value"), str)
    ):
        try:
            return str(uuid.UUID(value["value"]))
        except ValueError as exc:
            raise InvalidBackup("Das Backup enthält eine ungültige UUID") from exc
    raise InvalidBackup("Das Backup enthält eine ungültige UUID")


def _validate_category_graph(rows: Sequence[object]) -> None:
    parents: dict[str, str | None] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise InvalidBackup("Die Kategoriedaten des Backups sind ungültig")
        identifier = _typed_uuid(item.get("id"))
        if identifier is None or identifier in parents:
            raise InvalidBackup("Das Backup enthält ungültige oder doppelte Kategorien")
        parents[identifier] = _typed_uuid(item.get("parent_id"))
    for identifier, parent in parents.items():
        if parent is not None and parent not in parents:
            raise InvalidBackup("Eine Kategorie verweist auf ein fehlendes Elternelement")
        seen = {identifier}
        current = parent
        while current is not None:
            if current in seen:
                raise InvalidBackup("Der Kategoriebaum enthält einen Zyklus")
            seen.add(current)
            current = parents[current]


def _canonical_relational_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_column_value(value: object, python_type: type[object]) -> None:
    if value is None:
        return
    if python_type is uuid.UUID:
        _typed_uuid(value)
    elif python_type is datetime:
        if not (
            isinstance(value, dict)
            and value.get("$type") == "datetime"
            and isinstance(value.get("value"), str)
        ):
            raise InvalidBackup("Das Backup enthält einen ungültigen Zeitstempel")
        try:
            datetime.fromisoformat(value["value"])
        except ValueError as exc:
            raise InvalidBackup("Das Backup enthält einen ungültigen Zeitstempel") from exc
    elif python_type is Decimal:
        if not (
            isinstance(value, dict)
            and value.get("$type") == "decimal"
            and isinstance(value.get("value"), str)
        ):
            raise InvalidBackup("Das Backup enthält einen ungültigen Dezimalwert")
        try:
            Decimal(value["value"])
        except InvalidOperation as exc:
            raise InvalidBackup("Das Backup enthält einen ungültigen Dezimalwert") from exc


def _validate_relational_tables(tables: Mapping[str, object]) -> None:
    """Validate the complete exported relationship graph without mutating the DB.

    PostgreSQL still validates CHECK/expression indexes during the restore's
    pre-switch transaction. This pass catches structural, PK, unique and FK
    failures at upload time and ensures every Core insert has the required data.
    """
    model_by_table = {table_name(model): model for model in BACKUP_MODELS}
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    keys_by_table_column: dict[tuple[str, str], set[str]] = {}

    for name, model in model_by_table.items():
        raw_rows = tables.get(name)
        if not isinstance(raw_rows, list):
            raise InvalidBackup(f"Die Tabelle {name} ist ungültig")
        table = cast(Table, model.__table__)
        known_columns = {column.name for column in table.columns}
        rows: list[dict[str, Any]] = []
        primary_key_columns = [column.name for column in table.primary_key.columns]
        seen_primary_keys: set[tuple[str, ...]] = set()

        for raw_row in raw_rows:
            if not isinstance(raw_row, dict) or any(not isinstance(key, str) for key in raw_row):
                raise InvalidBackup(f"Die Tabelle {name} enthält eine ungültige Zeile")
            unknown = set(raw_row) - known_columns
            if unknown:
                raise InvalidBackup(f"Die Tabelle {name} enthält unbekannte Spalten")
            for column in table.columns:
                if column.name not in raw_row:
                    if column.primary_key or (
                        not column.nullable
                        and column.default is None
                        and column.server_default is None
                    ):
                        raise InvalidBackup(
                            f"Der Tabelle {name} fehlt die Pflichtspalte {column.name}"
                        )
                    continue
                value = raw_row[column.name]
                if value is None and not column.nullable:
                    raise InvalidBackup(
                        f"Die Pflichtspalte {name}.{column.name} darf nicht leer sein"
                    )
                try:
                    python_type = column.type.python_type
                except (AttributeError, NotImplementedError):
                    python_type = object
                _validate_column_value(value, python_type)

            primary_key = tuple(
                _canonical_relational_value(raw_row[column]) for column in primary_key_columns
            )
            if primary_key in seen_primary_keys:
                raise InvalidBackup(f"Die Tabelle {name} enthält doppelte Primärschlüssel")
            seen_primary_keys.add(primary_key)
            rows.append(raw_row)

        rows_by_table[name] = rows
        for column in table.columns:
            keys_by_table_column[(name, column.name)] = {
                _canonical_relational_value(row[column.name])
                for row in rows
                if column.name in row and row[column.name] is not None
            }

        unique_groups: list[tuple[str, ...]] = [
            (column.name,) for column in table.columns if column.unique and not column.primary_key
        ]
        unique_groups.extend(
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        for group in unique_groups:
            seen_unique: set[tuple[str, ...]] = set()
            for row in rows:
                if any(column not in row or row[column] is None for column in group):
                    continue
                value = tuple(_canonical_relational_value(row[column]) for column in group)
                if value in seen_unique:
                    raise InvalidBackup(f"Die Tabelle {name} verletzt eine Eindeutigkeitsbeziehung")
                seen_unique.add(value)

    for name, model in model_by_table.items():
        table = cast(Table, model.__table__)
        for foreign_key in table.foreign_keys:
            target = foreign_key.column
            target_values = keys_by_table_column[(target.table.name, target.name)]
            for row in rows_by_table[name]:
                value = row.get(foreign_key.parent.name)
                if value is None:
                    continue
                if _canonical_relational_value(value) not in target_values:
                    raise InvalidBackup(
                        f"Die Fremdschlüsselbeziehung {name}.{foreign_key.parent.name} ist ungültig"
                    )


def preflight_backup(path: Path, settings: Settings | None = None) -> PreflightResult:
    settings = settings or get_settings()
    if not path.is_file() or path.stat().st_size > settings.max_backup_upload_bytes:
        raise InvalidBackup("Das Backup fehlt oder überschreitet die erlaubte Größe")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 100_000:
                raise InvalidBackup("Das Archiv enthält zu viele Dateien")
            seen: set[str] = set()
            exact_names: set[str] = set()
            total_uncompressed = 0
            for entry in entries:
                name = safe_archive_name(entry.filename)
                folded = name.casefold()
                if folded in seen:
                    raise InvalidBackup("Das Archiv enthält kollidierende Dateinamen")
                seen.add(folded)
                exact_names.add(name)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                    raise InvalidBackup("Das Archiv enthält unzulässige Dateiverweise")
                if entry.flag_bits & 0x1:
                    raise InvalidBackup("Verschlüsselte Archive werden nicht unterstützt")
                total_uncompressed += entry.file_size
                if total_uncompressed > settings.max_backup_upload_bytes * 3:
                    raise InvalidBackup("Das Archiv würde zu viele Daten entpacken")
                if entry.compress_size and entry.file_size / entry.compress_size > 200:
                    raise InvalidBackup("Das Archiv enthält verdächtig stark komprimierte Daten")

            required = {"manifest.json", "application-data.json", "checksums.sha256"}
            if not required.issubset(exact_names):
                raise InvalidBackup("Dem Archiv fehlen erforderliche Dateien")
            for entry in entries:
                if entry.filename.endswith("/"):
                    continue
                if entry.filename not in required and not entry.filename.startswith("media/"):
                    raise InvalidBackup("Das Archiv enthält unerwartete Dateien")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                raise InvalidBackup("Das Backup-Manifest ist ungewöhnlich groß")
            if archive.getinfo("checksums.sha256").file_size > MAX_CHECKSUM_BYTES:
                raise InvalidBackup("Die Prüfsummenliste ist ungewöhnlich groß")
            if archive.getinfo("application-data.json").file_size > MAX_APPLICATION_DATA_BYTES:
                raise InvalidBackup("Die Anwendungsdaten sind ungewöhnlich groß")
            checksums = _parse_checksums(archive.read("checksums.sha256"))
            expected_checksum_names = exact_names - {"checksums.sha256"}
            expected_checksum_names = {
                name for name in expected_checksum_names if not name.endswith("/")
            }
            if set(checksums) != expected_checksum_names:
                raise InvalidBackup("Die Prüfsummenliste ist unvollständig oder enthält Zusätze")
            for name, expected in checksums.items():
                if name not in archive.namelist():
                    raise InvalidBackup(f"Die Datei {name} fehlt")
                digest = hashlib.sha256()
                with archive.open(name) as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise InvalidBackup(f"Die Prüfsumme von {name} stimmt nicht")

            manifest = BackupManifest.model_validate_json(archive.read("manifest.json"))
            source_schema_version = manifest.database_schema_version
            if source_schema_version not in SUPPORTED_DATABASE_SCHEMA_VERSIONS:
                raise InvalidBackup("Diese Datenbankschemaversion wird nicht unterstützt")
            application = json.loads(archive.read("application-data.json"))
            if not isinstance(application, dict):
                raise InvalidBackup("Die Anwendungsdaten sind ungültig")
            if application.get("format") != "rezeptverwaltung-application-data":
                raise InvalidBackup("Das Anwendungsdatenformat wird nicht unterstützt")
            if application.get("version") != "1.0":
                raise InvalidBackup("Diese Anwendungsdatenversion wird nicht unterstützt")
            if not isinstance(application.get("tables"), dict):
                raise InvalidBackup("Die Anwendungsdaten sind unvollständig")
            raw_tables = application["tables"]
            source_tables = expected_tables_for_schema(source_schema_version)
            if set(raw_tables) != source_tables or set(manifest.counts) != source_tables:
                raise InvalidBackup("Die Tabellenliste des Backups ist unvollständig")
            # Counts belong to the original archive contract and must be checked
            # before migration adds tables or columns.
            for table_key, rows in raw_tables.items():
                if not isinstance(rows, list) or manifest.counts[table_key] != len(rows):
                    raise InvalidBackup("Die Tabellenzählungen des Backups stimmen nicht")
            tables = normalize_tables(source_schema_version, raw_tables)
            _validate_category_graph(tables["categories"])
            if not any(
                row.get("role") == "admin" and row.get("is_active") is True
                for row in tables["users"]
                if isinstance(row, dict)
            ):
                raise InvalidBackup("Das Backup enthält keinen aktiven Administrator")
            actual_media = [
                name
                for name in archive.namelist()
                if name.startswith("media/") and not name.endswith("/")
            ]
            if len(actual_media) != manifest.media_file_count:
                raise InvalidBackup("Die Anzahl der Mediendateien stimmt nicht")
            for name in actual_media:
                if name not in checksums:
                    raise InvalidBackup(f"Für {name} fehlt eine Prüfsumme")
            expected_media: dict[str, dict[str, object]] = {}
            for row in tables["media_assets"]:
                if not isinstance(row, dict) or not isinstance(row.get("storage_key"), str):
                    raise InvalidBackup("Die Mediendaten des Backups sind ungültig")
                archive_name = f"media/{row['storage_key']}"
                safe_archive_name(archive_name)
                if archive_name in expected_media:
                    raise InvalidBackup("Das Backup enthält doppelte Speicherschlüssel")
                expected_media[archive_name] = row
            if set(actual_media) != set(expected_media):
                raise InvalidBackup("Mediendateien und Datenbankreferenzen stimmen nicht überein")
            for name, row in expected_media.items():
                if row.get("sha256") != checksums[name]:
                    raise InvalidBackup("Mediendatei und Datenbank-Prüfsumme stimmen nicht überein")
                if row.get("byte_size") != archive.getinfo(name).file_size:
                    raise InvalidBackup("Die gespeicherte Mediendateigröße stimmt nicht")
            if manifest.media_total_bytes != sum(
                archive.getinfo(name).file_size for name in actual_media
            ):
                raise InvalidBackup("Die Gesamtgröße der Mediendateien stimmt nicht")
            _validate_relational_tables(tables)
            warnings = []
            if source_schema_version != DATABASE_SCHEMA_VERSION:
                warnings.append(
                    f"Datenbankschema {source_schema_version} wird für die Wiederherstellung "
                    f"deterministisch auf {DATABASE_SCHEMA_VERSION} normalisiert."
                )
            if manifest.application_version != __version__:
                warnings.append(
                    "Das Backup stammt aus einer anderen Anwendungsversion; kompatible Migrationen werden angewendet."
                )
            normalized_counts = {name: len(rows) for name, rows in tables.items()}
            return PreflightResult(
                valid=True,
                backup_format_version=manifest.backup_format_version,
                application_version=manifest.application_version,
                created_at=manifest.created_at,
                counts=normalized_counts,
                media_file_count=manifest.media_file_count,
                media_total_bytes=manifest.media_total_bytes,
                required_disk_bytes=total_uncompressed * 2,
                source_database_schema_version=source_schema_version,
                warnings=warnings,
                normalized_tables=tables,
                media_checksums={name: checksums[name] for name in actual_media},
            )
    except zipfile.BadZipFile as exc:
        raise InvalidBackup("Die Datei ist kein gültiges ZIP-Backup") from exc
    except InvalidBackup:
        raise
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
        AttributeError,
        ValidationError,
    ) as exc:
        raise InvalidBackup("Das Backup enthält ungültige oder unvollständige Metadaten") from exc
