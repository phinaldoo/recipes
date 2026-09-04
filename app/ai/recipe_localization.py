from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from app.i18n import DEFAULT_LOCALE, Locale, translate
from app.schemas.ai import ExtractedRecipe
from app.schemas.recipe import IngredientGroupInput, IngredientInput, InstructionStepInput


@dataclass(frozen=True, slots=True)
class _UnitRule:
    target: str
    multiplier: Decimal = Decimal("1")
    approximate: bool = False


def _labels(target: str, *values: str) -> dict[str, _UnitRule]:
    rule = _UnitRule(target)
    return dict.fromkeys(values, rule)


def _converted_units(target: str, multiplier: str, *values: str) -> dict[str, _UnitRule]:
    rule = _UnitRule(target, Decimal(multiplier), approximate=True)
    return dict.fromkeys(values, rule)


_UNIT_RULES: Final[dict[str, _UnitRule]] = {
    **_labels("g", "g", "gram", "grams", "gramme", "grammes", "gramm"),
    **_labels(
        "kg",
        "kg",
        "kilogram",
        "kilograms",
        "kilogramme",
        "kilogrammes",
        "kilogramm",
    ),
    **_labels(
        "ml",
        "ml",
        "milliliter",
        "milliliters",
        "millilitre",
        "millilitres",
    ),
    **_labels("l", "l", "liter", "liters", "litre", "litres"),
    **_labels(
        "TL",
        "tsp",
        "teaspoon",
        "teaspoons",
        "teelöffel",
        "teeloeffel",
        "cucharadita",
        "cucharaditas",
        "茶匙",
        "छोटा चम्मच",
        "छोटे चम्मच",
    ),
    **_labels(
        "EL",
        "tbsp",
        "tablespoon",
        "tablespoons",
        "esslöffel",
        "essloeffel",
        "cucharada",
        "cucharadas",
        "汤匙",
        "बड़ा चम्मच",
        "बड़े चम्मच",
    ),
    **_labels(
        "Stück",
        "piece",
        "pieces",
        "pc",
        "pcs",
        "stück",
        "stueck",
        "unidad",
        "unidades",
        "个",
        "टुकड़ा",
        "टुकड़े",
    ),
    **_labels(
        "Prise",
        "pinch",
        "pinches",
        "prise",
        "prisen",
        "pizca",
        "pizcas",
        "撮",
        "चुटकी",
    ),
    **_labels("Zehe", "clove", "cloves", "zehe", "zehen", "diente", "dientes", "瓣", "कली"),
    **_labels("Bund", "bunch", "bunches", "bund", "manojo", "manojos", "把", "गुच्छा"),
    **_labels(
        "Scheibe", "slice", "slices", "scheibe", "scheiben", "rodaja", "rodajas", "片", "स्लाइस"
    ),
    **_labels(
        "Dose", "can", "cans", "tin", "tins", "dose", "dosen", "lata", "latas", "罐", "डिब्बा"
    ),
    **_labels(
        "Packung",
        "package",
        "packages",
        "packet",
        "packets",
        "packung",
        "paquete",
        "paquetes",
        "包",
        "पैकेट",
    ),
    **_converted_units("ml", "236.5882365", "cup", "cups"),
    **_converted_units(
        "ml",
        "29.5735295625",
        "fl oz",
        "fl ounce",
        "fl ounces",
        "fluid oz",
        "fluid ounce",
        "fluid ounces",
    ),
    **_converted_units("g", "28.349523125", "oz", "ounce", "ounces"),
    **_converted_units("g", "453.59237", "lb", "lbs", "pound", "pounds"),
    **_converted_units("ml", "473.176473", "pt", "pint", "pints"),
    **_converted_units("ml", "946.352946", "qt", "quart", "quarts"),
    **_converted_units("l", "3.785411784", "gal", "gallon", "gallons"),
}

_FAHRENHEIT_SUFFIX = r"(?:°?\s*f|degrees?\s+fahrenheit|grad\s+fahrenheit)"
_FAHRENHEIT_RANGE_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?P<first>-?\d+(?:[.,]\d+)?)\s*(?:-|–|—|bis|to)\s*"
    rf"(?P<second>-?\d+(?:[.,]\d+)?)\s*{_FAHRENHEIT_SUFFIX}",
    re.IGNORECASE,
)
_FAHRENHEIT_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?P<value>-?\d+(?:[.,]\d+)?)\s*{_FAHRENHEIT_SUFFIX}",
    re.IGNORECASE,
)
_LOCALIZED_UNITS: Final[dict[Locale, dict[str, str]]] = {
    "de": {
        "TL": "TL",
        "EL": "EL",
        "Stück": "Stück",
        "Prise": "Prise",
        "Zehe": "Zehe",
        "Bund": "Bund",
        "Scheibe": "Scheibe",
        "Dose": "Dose",
        "Packung": "Packung",
    },
    "en": {
        "TL": "tsp",
        "EL": "tbsp",
        "Stück": "piece",
        "Prise": "pinch",
        "Zehe": "clove",
        "Bund": "bunch",
        "Scheibe": "slice",
        "Dose": "can",
        "Packung": "package",
    },
    "zh-CN": {
        "TL": "茶匙",
        "EL": "汤匙",
        "Stück": "个",
        "Prise": "撮",
        "Zehe": "瓣",
        "Bund": "把",
        "Scheibe": "片",
        "Dose": "罐",
        "Packung": "包",
    },
    "hi": {
        "TL": "छोटा चम्मच",
        "EL": "बड़ा चम्मच",
        "Stück": "टुकड़ा",
        "Prise": "चुटकी",
        "Zehe": "कली",
        "Bund": "गुच्छा",
        "Scheibe": "स्लाइस",
        "Dose": "डिब्बा",
        "Packung": "पैकेट",
    },
    "es": {
        "TL": "cucharadita",
        "EL": "cucharada",
        "Stück": "unidad",
        "Prise": "pizca",
        "Zehe": "diente",
        "Bund": "manojo",
        "Scheibe": "rodaja",
        "Dose": "lata",
        "Packung": "paquete",
    },
}


def _unit_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = normalized.replace(".", " ")
    return " ".join(normalized.split())


def _round_converted_amount(value: Decimal, target: str) -> Decimal:
    absolute = abs(value)
    if target in {"g", "ml"}:
        quantum = Decimal("1") if absolute >= 10 else Decimal("0.1")
    elif target in {"kg", "l"}:
        quantum = Decimal("0.01") if absolute >= 1 else Decimal("0.001")
    else:
        quantum = Decimal("0.0001")
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _convert_ingredient(
    ingredient: IngredientInput,
    target_language: Locale,
) -> tuple[IngredientInput, bool]:
    if not ingredient.unit:
        return ingredient, False
    rule = _UNIT_RULES.get(_unit_key(ingredient.unit))
    if rule is None:
        return ingredient, False
    if not rule.approximate:
        target = _LOCALIZED_UNITS[target_language].get(rule.target, rule.target)
        return ingredient.model_copy(update={"unit": target}), False

    values = [
        value * rule.multiplier
        for value in (ingredient.amount_min, ingredient.amount_max)
        if value is not None
    ]
    target = rule.target
    divisor = Decimal("1")
    if values and max(abs(value) for value in values) >= 1000:
        if target == "g":
            target = "kg"
            divisor = Decimal("1000")
        elif target == "ml":
            target = "l"
            divisor = Decimal("1000")

    def converted(value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return _round_converted_amount(value * rule.multiplier / divisor, target)

    return (
        ingredient.model_copy(
            update={
                "amount_min": converted(ingredient.amount_min),
                "amount_max": converted(ingredient.amount_max),
                "unit": target,
            }
        ),
        bool(values),
    )


def _fahrenheit_to_celsius(value: str) -> str:
    fahrenheit = Decimal(value.replace(",", "."))
    celsius = (fahrenheit - Decimal("32")) * Decimal("5") / Decimal("9")
    rounded = (celsius / Decimal("5")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal("5")
    return format(rounded, "f")


def _convert_temperatures(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    converted = False

    def replace_range(match: re.Match[str]) -> str:
        nonlocal converted
        converted = True
        first = _fahrenheit_to_celsius(match.group("first"))
        second = _fahrenheit_to_celsius(match.group("second"))
        return f"{first}–{second} °C"

    def replace_single(match: re.Match[str]) -> str:
        nonlocal converted
        converted = True
        return f"{_fahrenheit_to_celsius(match.group('value'))} °C"

    result = _FAHRENHEIT_RANGE_RE.sub(replace_range, value)
    result = _FAHRENHEIT_RE.sub(replace_single, result)
    return result, converted


def normalize_recipe_units(
    recipe: ExtractedRecipe,
    *,
    target_language: Locale = DEFAULT_LOCALE,
) -> ExtractedRecipe:
    """Apply a locale-aware deterministic safety net after model translation."""

    unit_converted = False
    temperature_converted = False
    groups: list[IngredientGroupInput] = []
    for group in recipe.ingredient_groups:
        ingredients: list[IngredientInput] = []
        for ingredient in group.ingredients:
            normalized, changed_unit = _convert_ingredient(ingredient, target_language)
            note, changed_temperature = _convert_temperatures(normalized.note)
            ingredients.append(normalized.model_copy(update={"note": note}))
            unit_converted = unit_converted or changed_unit
            temperature_converted = temperature_converted or changed_temperature
        groups.append(group.model_copy(update={"ingredients": ingredients}))

    description, description_changed = _convert_temperatures(recipe.description)
    notes, notes_changed = _convert_temperatures(recipe.notes)
    temperature_converted = temperature_converted or description_changed or notes_changed
    steps: list[InstructionStepInput] = []
    for step in recipe.instruction_steps:
        text, changed = _convert_temperatures(step.text)
        assert text is not None
        temperature_converted = temperature_converted or changed
        steps.append(step.model_copy(update={"text": text}))

    warnings = list(recipe.warnings)
    if unit_converted:
        warnings.append(translate(target_language, "ai.warning.nonmetric"))
    if temperature_converted:
        warnings.append(translate(target_language, "ai.warning.fahrenheit"))
    return recipe.model_copy(
        update={
            "description": description,
            "ingredient_groups": groups,
            "instruction_steps": steps,
            "notes": notes,
            "warnings": list(dict.fromkeys(warnings))[:100],
        }
    )


def normalize_german_recipe_units(recipe: ExtractedRecipe) -> ExtractedRecipe:
    """Backward-compatible wrapper for callers that explicitly expect German output."""

    return normalize_recipe_units(recipe, target_language="de")
