from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

DATABASE_SCHEMA_VERSION = "0012"


class BackupManifest(BaseModel):
    backup_format_version: Literal["1.0"] = "1.0"
    application_version: str
    database_schema_version: str
    created_at: datetime
    counts: dict[str, int]
    media_file_count: int
    media_total_bytes: int
    archive_contents: list[str] = Field(default_factory=list)


class PreflightResult(BaseModel):
    valid: bool
    backup_format_version: str
    application_version: str
    created_at: datetime
    counts: dict[str, int]
    media_file_count: int
    media_total_bytes: int
    required_disk_bytes: int
    source_database_schema_version: str = DATABASE_SCHEMA_VERSION
    warnings: list[str] = Field(default_factory=list)
    # These verified payloads deliberately never leave the server response.  A
    # restore consumes the exact in-memory representation produced by preflight
    # instead of opening and interpreting application-data.json a second time.
    normalized_tables: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict, exclude=True, repr=False
    )
    media_checksums: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
