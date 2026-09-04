from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener  # type: ignore[import-untyped]

from app.config import Settings, get_settings
from app.pdf_backend import PasswordProtectedPDF, PDFError, inspect_pdf

register_heif_opener()
Image.MAX_IMAGE_PIXELS = get_settings().max_image_pixels
logger = logging.getLogger(__name__)


class InvalidUpload(ValueError):
    pass


class StorageCapacityExceeded(InvalidUpload):
    pass


@dataclass(frozen=True)
class StoredFile:
    storage_key: str
    original_filename: str
    mime_type: str
    byte_size: int
    sha256: str
    width: int | None = None
    height: int | None = None
    page_count: int | None = None


def _validated_pdf_page_count(source: bytes | Path, settings: Settings) -> int:
    try:
        page_count = inspect_pdf(source).page_count
    except PasswordProtectedPDF as exc:
        raise InvalidUpload("Passwortgeschützte PDFs werden nicht unterstützt") from exc
    except PDFError as exc:
        raise InvalidUpload("Die PDF-Datei ist beschädigt") from exc
    if page_count > settings.max_pdf_pages:
        raise InvalidUpload("Das PDF enthält zu viele Seiten")
    return page_count


def active_storage_root(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    generations = settings.storage_root / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    current = settings.storage_root / "current"
    if not current.exists() and not current.is_symlink():
        bootstrap = generations / "bootstrap"
        bootstrap.mkdir(parents=True, exist_ok=True)
        with suppress(FileExistsError):
            os.symlink(Path("generations") / "bootstrap", current)
    root = current.resolve()
    if generations.resolve() not in root.parents:
        raise RuntimeError("Der aktive Medienspeicher zeigt auf ein ungültiges Ziel")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fsync_directory(path: Path) -> None:
    with suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Durably replace a small JSON state file on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.partial")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _journal_target(value: object, settings: Settings) -> Path:
    if not isinstance(value, str):
        raise ValueError("Restore-Journal enthält kein gültiges Generationenziel")
    generations = (settings.storage_root / "generations").resolve()
    target = Path(value).resolve()
    if generations not in target.parents or not target.is_dir():
        raise ValueError("Restore-Journal verweist auf eine ungültige Speichergeneration")
    return target


def recover_interrupted_restore(settings: Settings | None = None) -> bool:
    """Resolve a crash during the database/filesystem two-phase restore.

    The database commit writes `last_restore_id`. If that marker exists, the new
    generation is authoritative. Otherwise the old generation is restored.
    """
    settings = settings or get_settings()
    journal_path = settings.storage_root / ".restore-journal.json"
    if not journal_path.exists():
        return False
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if not isinstance(journal, dict):
            raise ValueError("Restore-Journal ist kein Objekt")
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("Restore-Journal ist beschädigt und wird zur manuellen Prüfung erhalten")
        return False
    restore_id = journal.get("restore_id")
    if not isinstance(restore_id, str) or not restore_id:
        logger.error("Restore-Journal enthält keine gültige Restore-ID")
        return False
    committed = False
    try:
        from app.database import SessionLocal
        from app.models import AppSetting

        with SessionLocal() as db:
            marker = db.get(AppSetting, "last_restore")
            committed = bool(marker and marker.value.get("restore_id") == restore_id)
    except Exception:
        logger.exception("Commit-Status des Restore-Journals konnte nicht bestimmt werden")
        return False
    try:
        target = _journal_target(
            journal.get("new_target") if committed else journal.get("old_target"), settings
        )
        swap_active_generation(target, settings=settings)
        journal_path.unlink()
        _fsync_directory(journal_path.parent)
        return True
    except Exception:
        logger.exception("Restore-Journal konnte nicht sicher abgeschlossen werden")
        return False


def cleanup_retained_files(settings: Settings | None = None) -> None:
    """Remove expired transient archives and inactive media generations safely."""
    settings = settings or get_settings()
    cutoff = time.time() - settings.backup_download_retention_hours * 3600
    backup_root = settings.backup_temp_root.resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    for candidate in backup_root.iterdir():
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.stat().st_mtime < cutoff and (
            candidate.suffix in {".zip", ".partial"} or candidate.name.startswith("restore-upload-")
        ):
            candidate.unlink(missing_ok=True)

    # During a crash-recovery journal both generations are intentionally retained.
    if (settings.storage_root / ".restore-journal.json").exists():
        return
    generations = (settings.storage_root / "generations").resolve()
    generations.mkdir(parents=True, exist_ok=True)
    active = active_storage_root(settings).resolve()
    for candidate in generations.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        if resolved == active or generations not in resolved.parents:
            continue
        if candidate.stat().st_mtime < cutoff:
            shutil.rmtree(candidate)


def swap_active_generation(target: Path, *, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    generations = (settings.storage_root / "generations").resolve()
    resolved = target.resolve()
    if generations not in resolved.parents:
        raise InvalidUpload("Ungültige Speichergeneration")
    relative = resolved.relative_to(settings.storage_root.resolve())
    current = settings.storage_root / "current"
    temporary = settings.storage_root / f".current-{secrets.token_hex(8)}"
    os.symlink(relative, temporary)
    os.replace(temporary, current)
    _fsync_directory(settings.storage_root)


MAGIC_TYPES: tuple[tuple[bytes, int, str, str], ...] = (
    (b"\xff\xd8\xff", 0, "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", 0, "image/png", ".png"),
    (b"GIF87a", 0, "image/gif", ".gif"),
    (b"GIF89a", 0, "image/gif", ".gif"),
    (b"%PDF-", 0, "application/pdf", ".pdf"),
)


def detect_type(header: bytes) -> tuple[str, str]:
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    for signature, offset, mime_type, extension in MAGIC_TYPES:
        if header[offset : offset + len(signature)] == signature:
            return mime_type, extension
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic", ".heic"
    raise InvalidUpload("Nicht unterstütztes Dateiformat")


def safe_download_name(filename: str) -> str:
    name = Path(filename).name.replace("\x00", "").strip()
    return name[:500] or "datei"


def ensure_storage_capacity(required_bytes: int, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if required_bytes < 0:
        raise ValueError("required_bytes darf nicht negativ sein")
    if settings.storage_min_free_bytes == 0:
        return
    root = active_storage_root(settings)
    free = shutil.disk_usage(root).free
    if free - required_bytes < settings.storage_min_free_bytes:
        raise StorageCapacityExceeded(
            "Der Medienspeicher hat nicht genügend freien Platz. Bitte räume Speicher frei."
        )


async def store_upload(
    upload: UploadFile,
    *,
    allowed: set[str] | None = None,
    max_bytes: int | None = None,
    settings: Settings | None = None,
) -> StoredFile:
    settings = settings or get_settings()
    limit = max_bytes or settings.max_upload_bytes
    declared_size = getattr(upload, "size", None)
    if isinstance(declared_size, int) and declared_size > limit:
        raise InvalidUpload("Die Datei überschreitet die erlaubte Größe")
    header = await upload.read(64)
    if len(header) > limit:
        raise InvalidUpload("Die Datei überschreitet die erlaubte Größe")
    mime_type, extension = detect_type(header)
    if allowed and mime_type not in allowed:
        raise InvalidUpload("Dieser Dateityp ist hier nicht erlaubt")
    ensure_storage_capacity(
        declared_size if isinstance(declared_size, int) else limit,
        settings,
    )

    bucket = "originals" if mime_type == "application/pdf" else "images"
    relative = Path(bucket) / secrets.token_hex(2) / f"{secrets.token_urlsafe(24)}{extension}"
    target = active_storage_root(settings) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".partial")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as handle:
            for chunk in (header,):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise InvalidUpload("Die Datei überschreitet die erlaubte Größe")
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        # The temporary file already consumes its final bytes. Recheck the
        # configured reserve before publishing it under the permanent key.
        ensure_storage_capacity(0, settings)

        width = height = page_count = None
        if mime_type.startswith("image/"):
            try:
                with Image.open(temporary) as image:
                    image.verify()
                with Image.open(temporary) as image:
                    width, height = image.size
                    if width * height > settings.max_image_pixels:
                        raise InvalidUpload("Das Bild enthält zu viele Bildpunkte")
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise InvalidUpload("Die Bilddatei ist beschädigt") from exc
        elif mime_type == "application/pdf":
            page_count = _validated_pdf_page_count(temporary, settings)

        os.replace(temporary, target)
        return StoredFile(
            storage_key=relative.as_posix(),
            original_filename=safe_download_name(upload.filename or "datei"),
            mime_type=mime_type,
            byte_size=size,
            sha256=digest.hexdigest(),
            width=width,
            height=height,
            page_count=page_count,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def resolve_storage_key(storage_key: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    root = active_storage_root(settings).resolve()
    candidate = (root / storage_key).resolve()
    if root not in candidate.parents:
        raise InvalidUpload("Ungültiger Speicherpfad")
    return candidate


def store_bytes(
    data: bytes,
    *,
    filename: str,
    kind: str,
    expected_sha256: str | None = None,
    settings: Settings | None = None,
) -> StoredFile:
    settings = settings or get_settings()
    if len(data) > settings.max_upload_bytes:
        raise InvalidUpload("Die Datei überschreitet die erlaubte Größe")
    ensure_storage_capacity(len(data), settings)
    mime_type, extension = detect_type(data[:64])
    if kind in {"recipe_image", "generated_image", "image_thumbnail"} and not mime_type.startswith(
        "image/"
    ):
        raise InvalidUpload("Für ein Rezeptbild ist eine gültige Bilddatei erforderlich")
    if kind == "url_snapshot_pdf" and mime_type != "application/pdf":
        raise InvalidUpload("Der URL-Snapshot ist keine gültige PDF-Datei")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 and not secrets.compare_digest(digest, expected_sha256):
        raise InvalidUpload("Die Prüfsumme der Datei stimmt nicht")
    width = height = page_count = None
    if mime_type.startswith("image/"):
        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if width * height > settings.max_image_pixels:
                    raise InvalidUpload("Das Bild enthält zu viele Bildpunkte")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise InvalidUpload("Die Bilddatei ist beschädigt") from exc
    elif mime_type == "application/pdf":
        page_count = _validated_pdf_page_count(data, settings)
    bucket = (
        "generated"
        if kind == "generated_image"
        else "derivatives"
        if kind == "image_thumbnail"
        else "imports"
    )
    relative = Path(bucket) / digest[:2] / f"{secrets.token_urlsafe(24)}{extension}"
    target = active_storage_root(settings) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".partial")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return StoredFile(
        storage_key=relative.as_posix(),
        original_filename=safe_download_name(filename),
        mime_type=mime_type,
        byte_size=len(data),
        sha256=digest,
        width=width,
        height=height,
        page_count=page_count,
    )
