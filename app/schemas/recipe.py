from __future__ import annotations

import base64
import binascii
import re
import unicodedata
import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

_FRACTION_PATTERN = re.compile(
    r"^(?:(?P<whole>\d+)\s+)?(?P<numerator>\d+)\s*/\s*(?P<denominator>\d+)$"
)
_VULGAR_FRACTIONS = {
    "¼": (1, 4),
    "½": (1, 2),
    "¾": (3, 4),
    "⅐": (1, 7),
    "⅑": (1, 9),
    "⅒": (1, 10),
    "⅓": (1, 3),
    "⅔": (2, 3),
    "⅕": (1, 5),
    "⅖": (2, 5),
    "⅗": (3, 5),
    "⅘": (4, 5),
    "⅙": (1, 6),
    "⅚": (5, 6),
    "⅛": (1, 8),
    "⅜": (3, 8),
    "⅝": (5, 8),
    "⅞": (7, 8),
}
_QUALITATIVE_INGREDIENT_AMOUNTS = frozenset({"etwas", "einige"})

RecipeKind = Literal["cooking", "baking"]
_BAKING_ROOT_NAMES = frozenset({"backen", "baking", "hornear", "烘焙", "बेकिंग"})


def infer_recipe_kind_from_categories(categories: object) -> RecipeKind:
    """Infer the legacy/default recipe kind from top-level category paths."""

    if not isinstance(categories, (list, tuple)):
        return "cooking"
    for category in categories:
        raw_path = (
            category.path
            if isinstance(category, CategoryPathInput)
            else category.get("path")
            if isinstance(category, dict)
            else None
        )
        if not isinstance(raw_path, (list, tuple)) or not raw_path:
            continue
        root = raw_path[0]
        if not isinstance(root, str):
            continue
        normalized = " ".join(unicodedata.normalize("NFKC", root).casefold().split())
        if normalized in _BAKING_ROOT_NAMES:
            return "baking"
    return "cooking"


def _fraction_decimal(value: str) -> Decimal | None:
    normalized = value.replace("⁄", "/")
    match = _FRACTION_PATTERN.fullmatch(normalized)
    if match:
        whole = int(match.group("whole") or 0)
        numerator = int(match.group("numerator"))
        denominator = int(match.group("denominator"))
    else:
        vulgar_fraction: tuple[int, int] | None = None
        for glyph, fraction in _VULGAR_FRACTIONS.items():
            if not normalized.endswith(glyph):
                continue
            whole_text = normalized.removesuffix(glyph).strip()
            if whole_text and not whole_text.isdecimal():
                return None
            whole = int(whole_text or 0)
            vulgar_fraction = fraction
            break
        if vulgar_fraction is None:
            return None
        numerator, denominator = vulgar_fraction

    if denominator == 0:
        raise ValueError("Ein Bruch darf keinen Nenner 0 haben")
    return (Decimal(whole) + Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str):
            value = " ".join(value.strip().split())
            fraction = _fraction_decimal(value)
            if fraction is not None:
                return fraction
            value = value.replace(" ", "").replace(",", ".")
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Bitte eine gültige Zahl eingeben") from exc


class IngredientInput(BaseModel):
    amount_min: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=4)
    amount_max: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=4)
    unit: str | None = Field(default=None, max_length=80)
    name: str = Field(min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=1000)
    is_scalable: bool = True

    _parse_min = field_validator("amount_min", mode="before")(parse_decimal)
    _parse_max = field_validator("amount_max", mode="before")(parse_decimal)

    @model_validator(mode="before")
    @classmethod
    def preserve_qualitative_amount(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        amount = value.get("amount_min")
        if not isinstance(amount, str):
            return value
        qualifier = " ".join(amount.strip().split())
        if qualifier.casefold().rstrip(".") not in _QUALITATIVE_INGREDIENT_AMOUNTS:
            return value

        normalized = dict(value)
        unit = normalized.get("unit")
        if unit is None or unit == "":
            normalized["unit"] = qualifier
        elif isinstance(unit, str):
            cleaned_unit = " ".join(unit.strip().split())
            if cleaned_unit.casefold() != qualifier.casefold():
                normalized["unit"] = f"{qualifier} {cleaned_unit}"
        normalized["amount_min"] = None
        normalized["is_scalable"] = False
        return normalized

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Der Zutatenname darf nicht leer sein")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> IngredientInput:
        if self.amount_max is not None and self.amount_min is None:
            raise ValueError("Für eine Höchstmenge ist auch eine Mindestmenge erforderlich")
        if (
            self.amount_max is not None
            and self.amount_min is not None
            and self.amount_max < self.amount_min
        ):
            raise ValueError("Die Höchstmenge darf nicht kleiner als die Mindestmenge sein")
        return self


class IngredientGroupInput(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    ingredients: list[IngredientInput] = Field(default_factory=list, max_length=300)


class InstructionStepInput(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)

    @field_validator("text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Der Zubereitungsschritt darf nicht leer sein")
        return value


class SourceInput(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    url: HttpUrl | None = None

    @model_validator(mode="after")
    def empty_to_none(self) -> SourceInput:
        if self.title is not None:
            self.title = self.title.strip() or None
        return self


class NutritionInput(BaseModel):
    basis: Literal["per_serving", "per_100g_ml"]
    energy_kj: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    energy_kcal: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    fat_g: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    saturated_fat_g: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    carbohydrates_g: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    sugars_g: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    fiber_g: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    protein_g: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    salt_g: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    note: str | None = Field(default=None, max_length=1000)

    _parse_decimals = field_validator(
        "energy_kj",
        "energy_kcal",
        "fat_g",
        "saturated_fat_g",
        "carbohydrates_g",
        "sugars_g",
        "fiber_g",
        "protein_g",
        "salt_g",
        mode="before",
    )(parse_decimal)

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None

    @model_validator(mode="after")
    def require_value(self) -> NutritionInput:
        fields = (
            self.energy_kj,
            self.energy_kcal,
            self.fat_g,
            self.saturated_fat_g,
            self.carbohydrates_g,
            self.sugars_g,
            self.fiber_g,
            self.protein_g,
            self.salt_g,
        )
        if all(value is None for value in fields):
            raise ValueError("Mindestens ein Nährwert muss angegeben werden")
        return self


class CategoryPathInput(BaseModel):
    id: uuid.UUID | None = None
    path: list[str] = Field(default_factory=list, min_length=1, max_length=20)
    origin: Literal["manual", "ai_import"] = "manual"

    @field_validator("path")
    @classmethod
    def clean_path(cls, value: list[str]) -> list[str]:
        cleaned = [part.strip() for part in value]
        if any(not part for part in cleaned):
            raise ValueError("Kategoriepfade dürfen keine leeren Bestandteile enthalten")
        if any(len(part) > 200 for part in cleaned):
            raise ValueError("Kategorienamen dürfen höchstens 200 Zeichen lang sein")
        return cleaned


class RecipeInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    recipe_kind: RecipeKind = "cooking"
    base_servings: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    serving_label: str = Field(default="Personen", min_length=1, max_length=80)
    prep_time_minutes: int | None = Field(default=None, ge=0, le=100_000)
    cook_time_minutes: int | None = Field(default=None, ge=0, le=100_000)
    rest_time_minutes: int | None = Field(default=None, ge=0, le=100_000)
    total_time_minutes: int | None = Field(default=None, ge=0, le=100_000)
    total_time_is_manual: bool = False
    nutrition: list[NutritionInput] = Field(default_factory=list, max_length=2)
    notes: str | None = Field(default=None, max_length=50_000)
    status: Literal["active", "archived"] = "active"
    ingredient_groups: list[IngredientGroupInput] = Field(default_factory=list, max_length=100)
    instruction_steps: list[InstructionStepInput] = Field(default_factory=list, max_length=300)
    categories: list[CategoryPathInput] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    source: SourceInput | None = None
    expected_updated_at: datetime | None = None

    _parse_servings = field_validator("base_servings", mode="before")(parse_decimal)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_recipe_kind(cls, value: object) -> object:
        if not isinstance(value, dict) or "recipe_kind" in value:
            return value
        enriched = dict(value)
        enriched["recipe_kind"] = infer_recipe_kind_from_categories(value.get("categories"))
        return enriched

    @field_validator("title", "serving_label")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Dieses Feld darf nicht leer sein")
        return value

    @field_validator("description", "notes")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            tag = " ".join(unicodedata.normalize("NFKC", item).strip().split())
            if not tag:
                continue
            if len(tag) > 100:
                raise ValueError("Ein Schlagwort darf höchstens 100 Zeichen lang sein")
            key = tag.casefold()
            if key not in seen:
                cleaned.append(tag)
                seen.add(key)
        return cleaned

    @model_validator(mode="after")
    def calculate_total(self) -> RecipeInput:
        if not self.total_time_is_manual:
            values = [self.prep_time_minutes, self.cook_time_minutes, self.rest_time_minutes]
            self.total_time_minutes = sum(value or 0 for value in values) or None
        unique_paths: set[tuple[str, ...]] = set()
        for category in self.categories:
            key = tuple(part.casefold() for part in category.path)
            if key in unique_paths:
                raise ValueError("Ein Kategoriepfad darf nur einmal ausgewählt werden")
            unique_paths.add(key)
        bases = [value.basis for value in self.nutrition]
        if len(set(bases)) != len(bases):
            raise ValueError("Jede Nährwert-Bezugsgröße darf nur einmal vorkommen")
        return self


class CommentInput(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)

    @field_validator("text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Die Notiz darf nicht leer sein")
        return value


class EncodedAsset(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    mime_type: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_base64: str
    kind: Literal["recipe_image", "original_upload", "url_snapshot_pdf", "generated_image"]
    caption: str | None = Field(default=None, max_length=1000)
    alt_text: str | None = Field(default=None, max_length=1000)
    is_cover: bool = False
    generation_metadata: dict[str, Any] | None = None

    def decoded(self, max_bytes: int) -> bytes:
        try:
            data = base64.b64decode(self.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{self.filename}: ungültige Base64-Daten") from exc
        if len(data) > max_bytes:
            raise ValueError(f"{self.filename}: Datei ist zu groß")
        return data


class ExportedComment(BaseModel):
    author_name: str
    author_email: str | None = None
    text: str
    created_at: datetime
    updated_at: datetime | None = None


class RecipePackageData(RecipeInput):
    comments: list[ExportedComment] = Field(default_factory=list, max_length=10_000)
    images: list[EncodedAsset] = Field(default_factory=list, max_length=100)
    original_assets: list[EncodedAsset] = Field(default_factory=list, max_length=100)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by_name: str | None = None
    updated_by_name: str | None = None

    @model_validator(mode="after")
    def validate_asset_roles(self) -> RecipePackageData:
        for asset in self.images:
            if asset.kind not in {"recipe_image", "generated_image"}:
                raise ValueError("Der Bilderbereich darf ausschließlich Rezeptbilder enthalten")
            if not asset.mime_type.startswith("image/"):
                raise ValueError("Ein Rezeptbild muss einen Bild-MIME-Typ verwenden")
        for asset in self.original_assets:
            if asset.kind not in {"original_upload", "url_snapshot_pdf"}:
                raise ValueError("Der Originalbereich enthält einen ungültigen Dateityp")
        return self


class RecipePackage(BaseModel):
    schema_version: Literal["1.1", "1.2", "1.3"]
    recipe: RecipePackageData


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: uuid.UUID | None = None


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: uuid.UUID | None = None
    position: int | None = Field(default=None, ge=0)


class CategoryMove(BaseModel):
    parent_id: uuid.UUID | None = None
    position: int = Field(default=0, ge=0)


class CategoryMerge(BaseModel):
    target_category_id: uuid.UUID


class ImageMetadataInput(BaseModel):
    position: int | None = Field(default=None, ge=0)
    is_cover: bool | None = None
    caption: str | None = Field(default=None, max_length=1000)
    alt_text: str | None = Field(default=None, max_length=1000)


class RestoreConfirmation(BaseModel):
    preflight_token: str = Field(min_length=32, max_length=256)
    confirmation: Annotated[
        str,
        Field(pattern=r"^(?:WIEDERHERSTELLEN|RESTORE|恢复|पुनर्स्थापित|RESTAURAR)$"),
    ]
    password: str = Field(min_length=1, max_length=1024)
