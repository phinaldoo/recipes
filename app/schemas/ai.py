from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.schemas.recipe import (
    IngredientGroupInput,
    InstructionStepInput,
    NutritionInput,
    RecipeKind,
)


class CategorySuggestion(BaseModel):
    path: list[str] = Field(min_length=1, max_length=12)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=1000)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: list[str]) -> list[str]:
        result = [part.strip() for part in value if part.strip()]
        if not result:
            raise ValueError("Ein Kategoriepfad darf nicht leer sein")
        return result


class NormalizedBoundingBox(BaseModel):
    """A source-relative box using stable 0..1000 coordinates."""

    left: int = Field(ge=0, le=999)
    top: int = Field(ge=0, le=999)
    right: int = Field(ge=1, le=1000)
    bottom: int = Field(ge=1, le=1000)

    @model_validator(mode="after")
    def validate_extent(self) -> NormalizedBoundingBox:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("Der Bildausschnitt muss eine positive Fläche haben")
        return self


def _full_page_box() -> NormalizedBoundingBox:
    return NormalizedBoundingBox(left=0, top=0, right=1000, bottom=1000)


class RecipeSourceRegion(BaseModel):
    page: int = Field(ge=1)
    bounding_box: NormalizedBoundingBox = Field(default_factory=_full_page_box)


class RecipeImageCandidate(BaseModel):
    page: int = Field(ge=1)
    bounding_box: NormalizedBoundingBox = Field(default_factory=_full_page_box)
    description: str = Field(max_length=1000)
    confidence: float = Field(ge=0, le=1)


class DetectedRecipe(BaseModel):
    title_hint: str = Field(min_length=1, max_length=300)
    source_regions: list[RecipeSourceRegion] = Field(min_length=1, max_length=20)
    recipe_image_candidates: list[RecipeImageCandidate] = Field(default_factory=list, max_length=10)
    warnings: list[str] = Field(default_factory=list, max_length=50)
    detection_confidence: Literal["high", "medium", "low"] = "medium"


class DetectedRecipeDocument(BaseModel):
    recipes: list[DetectedRecipe] = Field(min_length=1, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class RecipeImageMatch(BaseModel):
    matches_recipe: bool
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=1000)


class _ExtractedRecipeContent(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    recipe_kind: RecipeKind = "cooking"
    prep_time_minutes: int | None = Field(default=None, ge=0, le=100_000)
    cook_time_minutes: int | None = Field(default=None, ge=0, le=100_000)
    rest_time_minutes: int | None = Field(default=None, ge=0, le=100_000)
    total_time_minutes: int | None = Field(default=None, ge=0, le=100_000)
    nutrition: list[NutritionInput] = Field(default_factory=list, max_length=2)
    ingredient_groups: list[IngredientGroupInput] = Field(default_factory=list, max_length=100)
    instruction_steps: list[InstructionStepInput] = Field(default_factory=list, max_length=300)
    category_suggestions: list[CategorySuggestion] = Field(default_factory=list, max_length=20)
    notes: str | None = Field(default=None, max_length=50_000)
    source_title: str | None = Field(default=None, max_length=500)
    source_url: HttpUrl | None = None
    source_regions: list[RecipeSourceRegion] = Field(default_factory=list, max_length=20)
    has_recipe_image: bool = False
    recipe_image_candidates: list[RecipeImageCandidate] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    extraction_confidence: Literal["high", "medium", "low"] = "medium"


class ExtractedRecipeDraft(_ExtractedRecipeContent):
    """Nullable, source-faithful shape used only at the AI boundary.

    A source can omit its yield or express it as a range. Those are valid
    extraction results even though the persisted recipe model requires one
    positive scaling basis.
    """

    servings_min: float | None = Field(default=None, gt=0, le=100_000)
    servings_max: float | None = Field(default=None, gt=0, le=100_000)
    serving_label: str | None = Field(default=None, min_length=1, max_length=80)
    serving_text: str | None = Field(default=None, max_length=500)


class ExtractedRecipe(_ExtractedRecipeContent):
    """Validated, domain-ready recipe returned to the import pipeline."""

    base_servings: Decimal = Field(default=Decimal("4"), gt=0)
    serving_label: str = Field(default="Personen", min_length=1, max_length=80)
