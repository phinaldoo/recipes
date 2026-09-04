from __future__ import annotations

import unicodedata
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


def _clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def _clean_bounded(value: str, *, maximum: int, empty_message: str) -> str:
    cleaned = _clean(value)
    if not cleaned:
        raise ValueError(empty_message)
    if len(cleaned) > maximum:
        raise ValueError(f"Nach der Normalisierung sind maximal {maximum} Zeichen erlaubt")
    return cleaned


class TagPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_bounded(
            value,
            maximum=100,
            empty_message="Bitte gib ein Schlagwort ein",
        )


class SynonymPayload(BaseModel):
    term: str = Field(min_length=1, max_length=100)
    synonym: str = Field(min_length=1, max_length=100)

    @field_validator("term", "synonym")
    @classmethod
    def clean_value(cls, value: str) -> str:
        return _clean_bounded(
            value,
            maximum=100,
            empty_message="Bitte gib einen Suchbegriff ein",
        )

    @model_validator(mode="after")
    def distinct_values(self) -> SynonymPayload:
        if self.term.casefold() == self.synonym.casefold():
            raise ValueError("Suchbegriff und Synonym müssen verschieden sein")
        return self


class ShareCreateInput(BaseModel):
    expires_in_days: int | None = Field(default=30, ge=1, le=365)


class VersionRestoreInput(BaseModel):
    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Der Änderungszeitpunkt muss eine Zeitzone enthalten")
        return value
