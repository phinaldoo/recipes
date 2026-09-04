from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str | None = None


class Pagination(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=24, ge=1, le=100)
    total: int
    pages: int


class UserPublic(ORMModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    created_at: datetime

    @property
    def visible_name(self) -> str:
        return self.display_name or self.email
