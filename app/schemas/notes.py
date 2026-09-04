from __future__ import annotations

import unicodedata
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


def _optional_single_line(value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise ValueError(f"Nach der Normalisierung sind maximal {maximum} Zeichen erlaubt")
    return cleaned


class NotePayload(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=2048)
    content: str | None = Field(default=None, max_length=10_000)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        return _optional_single_line(value, maximum=200)

    @field_validator("url")
    @classmethod
    def clean_url(cls, value: str | None) -> str | None:
        cleaned = _optional_single_line(value, maximum=2048)
        if cleaned is None:
            return None
        if any(ord(character) < 32 for character in cleaned):
            raise ValueError("Die Webadresse enthält ungültige Zeichen")
        try:
            parsed = urlsplit(cleaned)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("Bitte gib eine gültige Webadresse ein") from exc
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Bitte gib eine vollständige http- oder https-Webadresse ein")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Webadressen mit Zugangsdaten werden nicht unterstützt")
        return cleaned

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
        cleaned = cleaned.strip()
        if not cleaned:
            return None
        if len(cleaned) > 10_000:
            raise ValueError("Nach der Normalisierung sind maximal 10000 Zeichen erlaubt")
        return cleaned

    @model_validator(mode="after")
    def require_content(self) -> NotePayload:
        if self.title is None and self.url is None and self.content is None:
            raise ValueError("Bitte gib einen Titel, eine Webadresse oder eine Notiz ein")
        return self
