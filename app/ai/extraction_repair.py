from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import HttpUrl, TypeAdapter, ValidationError

from app.i18n import DEFAULT_LOCALE, Locale, translate
from app.schemas.ai import ExtractedRecipe, ExtractedRecipeDraft
from app.schemas.recipe import (
    IngredientInput,
    NutritionInput,
    infer_recipe_kind_from_categories,
    parse_decimal,
)

_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
_NUMBER_PATTERN_FRAGMENT = (
    r"(?:(?:(?:\d+)\s+)?\d+\s*[\/⁄]\s*\d+|"
    r"(?:\d+\s*)?[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]|\d+(?:[.,]\d+)?)"
)
_RANGE_PATTERN = re.compile(
    rf"^\s*(?P<minimum>{_NUMBER_PATTERN_FRAGMENT})\s*(?:-|–|—|bis|to)\s*"
    rf"(?P<maximum>{_NUMBER_PATTERN_FRAGMENT})(?P<suffix>.*)$",
    re.IGNORECASE,
)
_SCALAR_PATTERN = re.compile(
    rf"^\s*(?:ca\.?|circa|etwa|approx\.?)?\s*"
    rf"(?P<value>{_NUMBER_PATTERN_FRAGMENT})(?P<suffix>.*)$",
    re.IGNORECASE,
)
_MINUTES_PATTERN = re.compile(
    r"^\s*(?:ca\.?|circa|etwa|approx\.?)?\s*(?P<value>\d+)\s*"
    r"(?:min(?:ute|uten|utes?)?\.?)?\s*$",
    re.IGNORECASE,
)
_URL_ADAPTER = TypeAdapter(HttpUrl)
_NUTRITION_FIELDS = (
    "energy_kj",
    "energy_kcal",
    "fat_g",
    "saturated_fat_g",
    "carbohydrates_g",
    "sugars_g",
    "fiber_g",
    "protein_g",
    "salt_g",
)

MISSING_SERVINGS_WARNING = (
    "In der Quelle wurde keine eindeutige Ausbeute erkannt. Das vollständige Rezept wird "
    "deshalb als 1 Rezept gespeichert; die Zutatenmengen bleiben unverändert."
)
RANGE_SERVINGS_WARNING = (
    "Die Quelle nennt die Ausbeute als Bereich. Damit keine Portionsgröße erfunden wird, "
    "wird das vollständige Rezept als 1 Rezept gespeichert; die Zutatenmengen bleiben unverändert."
)

_REPAIR_WARNINGS: dict[Locale, dict[str, str]] = {
    "de": {},
    "en": {
        "Ungültige Kategorie-Vorschläge wurden ignoriert.": "Invalid category suggestions were ignored.",
        "Einige ungültige oder doppelte Kategorie-Vorschläge wurden ignoriert.": "Some invalid or duplicate category suggestions were ignored.",
        "Ungültige Nährwertangaben wurden weggelassen.": "Invalid nutrition information was omitted.",
        "Eine ungültige Nährwertzeile wurde weggelassen.": "An invalid nutrition row was omitted.",
        "Eine ungültige oder doppelte Nährwertzeile wurde weggelassen.": "An invalid or duplicate nutrition row was omitted.",
        "Eine unvollständige Nährwertzeile wurde weggelassen.": "An incomplete nutrition row was omitted.",
        "Ein ungültiger einzelner Nährwert wurde weggelassen.": "An invalid nutrition value was omitted.",
        "Eine unlesbare Zutat wurde weggelassen.": "An unreadable ingredient was omitted.",
        "Eine Zutat ohne erkennbaren Namen wurde weggelassen.": "An ingredient without a recognizable name was omitted.",
        "Eine vertauschte Zutatenmenge wurde als Einzelmenge übernommen.": "A reversed ingredient amount was retained as a single amount.",
        "Eine vertauschte Mengenspanne wurde korrigiert.": "A reversed amount range was corrected.",
        "Eine nicht numerische Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.": "A non-numeric ingredient amount was retained as a note and will not be scaled.",
        "Eine ungültige Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.": "An invalid ingredient amount was retained as a note and will not be scaled.",
        "Ungültige Zutatenlisten wurden weggelassen.": "Invalid ingredient lists were omitted.",
        "Eine unlesbare Zutatengruppe wurde weggelassen.": "An unreadable ingredient group was omitted.",
        "Eine Zutatengruppe ohne lesbare Zutaten wurde weggelassen.": "An ingredient group without readable ingredients was omitted.",
        "Überzählige Zutatengruppen wurden weggelassen.": "Excess ingredient groups were omitted.",
        "Ungültige Zubereitungsschritte wurden weggelassen.": "Invalid instruction steps were omitted.",
        "Ein leerer Zubereitungsschritt wurde weggelassen.": "An empty instruction step was omitted.",
        "Überzählige Zubereitungsschritte wurden weggelassen.": "Excess instruction steps were omitted.",
        "Eine unvollständige Ausbeute wurde als Einzelwert übernommen.": "An incomplete yield was retained as a single value.",
        "Eine vertauschte Ausbeute-Spanne wurde korrigiert.": "A reversed yield range was corrected.",
        "Eine zu kleine Ausbeute konnte nicht als Skalierungsbasis verwendet werden.": "A yield that was too small could not be used as the scaling basis.",
        "Die Ausbeute wurde auf drei Nachkommastellen gerundet.": "The yield was rounded to three decimal places.",
        "Der erkannte Dokumenttitel wurde verwendet, weil der Rezepttitel fehlte.": "The detected document title was used because the recipe title was missing.",
        "Ein zu langer Rezepttitel wurde gekürzt.": "An overly long recipe title was shortened.",
        "Eine ungültige Quellen-URL wurde weggelassen.": "An invalid source URL was omitted.",
    },
    "zh-CN": {
        "Ungültige Kategorie-Vorschläge wurden ignoriert.": "已忽略无效的分类建议。",
        "Einige ungültige oder doppelte Kategorie-Vorschläge wurden ignoriert.": "已忽略部分无效或重复的分类建议。",
        "Ungültige Nährwertangaben wurden weggelassen.": "已省略无效的营养信息。",
        "Eine ungültige Nährwertzeile wurde weggelassen.": "已省略一行无效的营养信息。",
        "Eine ungültige oder doppelte Nährwertzeile wurde weggelassen.": "已省略一行无效或重复的营养信息。",
        "Eine unvollständige Nährwertzeile wurde weggelassen.": "已省略一行不完整的营养信息。",
        "Ein ungültiger einzelner Nährwert wurde weggelassen.": "已省略一个无效的营养值。",
        "Eine unlesbare Zutat wurde weggelassen.": "已省略无法辨认的食材。",
        "Eine Zutat ohne erkennbaren Namen wurde weggelassen.": "已省略名称无法识别的食材。",
        "Eine vertauschte Zutatenmenge wurde als Einzelmenge übernommen.": "顺序颠倒的食材用量已作为单一数值保留。",
        "Eine vertauschte Mengenspanne wurde korrigiert.": "已纠正顺序颠倒的用量范围。",
        "Eine nicht numerische Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.": "非数字食材用量已作为备注保留，不会随份量缩放。",
        "Eine ungültige Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.": "无效食材用量已作为备注保留，不会随份量缩放。",
        "Ungültige Zutatenlisten wurden weggelassen.": "已省略无效的食材列表。",
        "Eine unlesbare Zutatengruppe wurde weggelassen.": "已省略无法辨认的食材组。",
        "Eine Zutatengruppe ohne lesbare Zutaten wurde weggelassen.": "已省略没有可辨认食材的食材组。",
        "Überzählige Zutatengruppen wurden weggelassen.": "已省略超出限制的食材组。",
        "Ungültige Zubereitungsschritte wurden weggelassen.": "已省略无效的制作步骤。",
        "Ein leerer Zubereitungsschritt wurde weggelassen.": "已省略空白的制作步骤。",
        "Überzählige Zubereitungsschritte wurden weggelassen.": "已省略超出限制的制作步骤。",
        "Eine unvollständige Ausbeute wurde als Einzelwert übernommen.": "不完整的份量已作为单一数值保留。",
        "Eine vertauschte Ausbeute-Spanne wurde korrigiert.": "已纠正顺序颠倒的份量范围。",
        "Eine zu kleine Ausbeute konnte nicht als Skalierungsbasis verwendet werden.": "过小的份量无法用作缩放基准。",
        "Die Ausbeute wurde auf drei Nachkommastellen gerundet.": "份量已四舍五入到三位小数。",
        "Der erkannte Dokumenttitel wurde verwendet, weil der Rezepttitel fehlte.": "因食谱标题缺失，已使用识别到的文档标题。",
        "Ein zu langer Rezepttitel wurde gekürzt.": "过长的食谱标题已缩短。",
        "Eine ungültige Quellen-URL wurde weggelassen.": "已省略无效的来源网址。",
    },
    "hi": {
        "Ungültige Kategorie-Vorschläge wurden ignoriert.": "अमान्य श्रेणी सुझावों को अनदेखा किया गया।",
        "Einige ungültige oder doppelte Kategorie-Vorschläge wurden ignoriert.": "कुछ अमान्य या दोहराए गए श्रेणी सुझावों को अनदेखा किया गया।",
        "Ungültige Nährwertangaben wurden weggelassen.": "अमान्य पोषण जानकारी हटा दी गई।",
        "Eine ungültige Nährwertzeile wurde weggelassen.": "एक अमान्य पोषण पंक्ति हटा दी गई।",
        "Eine ungültige oder doppelte Nährwertzeile wurde weggelassen.": "एक अमान्य या दोहराई गई पोषण पंक्ति हटा दी गई।",
        "Eine unvollständige Nährwertzeile wurde weggelassen.": "एक अधूरी पोषण पंक्ति हटा दी गई।",
        "Ein ungültiger einzelner Nährwert wurde weggelassen.": "एक अमान्य पोषण मान हटा दिया गया।",
        "Eine unlesbare Zutat wurde weggelassen.": "अपठनीय सामग्री हटा दी गई।",
        "Eine Zutat ohne erkennbaren Namen wurde weggelassen.": "पहचान योग्य नाम के बिना सामग्री हटा दी गई।",
        "Eine vertauschte Zutatenmenge wurde als Einzelmenge übernommen.": "उलटी सामग्री मात्रा को एकल मात्रा के रूप में रखा गया।",
        "Eine vertauschte Mengenspanne wurde korrigiert.": "उलटी मात्रा सीमा ठीक की गई।",
        "Eine nicht numerische Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.": "गैर-संख्यात्मक मात्रा नोट के रूप में रखी गई और स्केल नहीं होगी।",
        "Eine ungültige Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.": "अमान्य मात्रा नोट के रूप में रखी गई और स्केल नहीं होगी।",
        "Ungültige Zutatenlisten wurden weggelassen.": "अमान्य सामग्री सूचियाँ हटा दी गईं।",
        "Eine unlesbare Zutatengruppe wurde weggelassen.": "अपठनीय सामग्री समूह हटा दिया गया।",
        "Eine Zutatengruppe ohne lesbare Zutaten wurde weggelassen.": "पठनीय सामग्री के बिना समूह हटा दिया गया।",
        "Überzählige Zutatengruppen wurden weggelassen.": "अतिरिक्त सामग्री समूह हटा दिए गए।",
        "Ungültige Zubereitungsschritte wurden weggelassen.": "अमान्य विधि चरण हटा दिए गए।",
        "Ein leerer Zubereitungsschritt wurde weggelassen.": "खाली विधि चरण हटा दिया गया।",
        "Überzählige Zubereitungsschritte wurden weggelassen.": "अतिरिक्त विधि चरण हटा दिए गए।",
        "Eine unvollständige Ausbeute wurde als Einzelwert übernommen.": "अधूरी मात्रा को एकल मान के रूप में रखा गया।",
        "Eine vertauschte Ausbeute-Spanne wurde korrigiert.": "उलटी मात्रा सीमा ठीक की गई।",
        "Eine zu kleine Ausbeute konnte nicht als Skalierungsbasis verwendet werden.": "बहुत छोटी मात्रा को स्केलिंग आधार नहीं बनाया जा सका।",
        "Die Ausbeute wurde auf drei Nachkommastellen gerundet.": "मात्रा को तीन दशमलव स्थानों तक राउंड किया गया।",
        "Der erkannte Dokumenttitel wurde verwendet, weil der Rezepttitel fehlte.": "रेसिपी शीर्षक न होने पर पहचाना गया दस्तावेज़ शीर्षक उपयोग किया गया।",
        "Ein zu langer Rezepttitel wurde gekürzt.": "बहुत लंबा रेसिपी शीर्षक छोटा किया गया।",
        "Eine ungültige Quellen-URL wurde weggelassen.": "अमान्य स्रोत URL हटा दिया गया।",
    },
    "es": {
        "Ungültige Kategorie-Vorschläge wurden ignoriert.": "Se ignoraron las sugerencias de categoría no válidas.",
        "Einige ungültige oder doppelte Kategorie-Vorschläge wurden ignoriert.": "Se ignoraron algunas sugerencias de categoría no válidas o duplicadas.",
        "Ungültige Nährwertangaben wurden weggelassen.": "Se omitió información nutricional no válida.",
        "Eine ungültige Nährwertzeile wurde weggelassen.": "Se omitió una fila nutricional no válida.",
        "Eine ungültige oder doppelte Nährwertzeile wurde weggelassen.": "Se omitió una fila nutricional no válida o duplicada.",
        "Eine unvollständige Nährwertzeile wurde weggelassen.": "Se omitió una fila nutricional incompleta.",
        "Ein ungültiger einzelner Nährwert wurde weggelassen.": "Se omitió un valor nutricional no válido.",
        "Eine unlesbare Zutat wurde weggelassen.": "Se omitió un ingrediente ilegible.",
        "Eine Zutat ohne erkennbaren Namen wurde weggelassen.": "Se omitió un ingrediente sin nombre reconocible.",
        "Eine vertauschte Zutatenmenge wurde als Einzelmenge übernommen.": "Una cantidad invertida se conservó como valor único.",
        "Eine vertauschte Mengenspanne wurde korrigiert.": "Se corrigió un intervalo de cantidades invertido.",
        "Eine nicht numerische Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.": "Una cantidad no numérica se conservó como nota y no se escalará.",
        "Eine ungültige Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.": "Una cantidad no válida se conservó como nota y no se escalará.",
        "Ungültige Zutatenlisten wurden weggelassen.": "Se omitieron listas de ingredientes no válidas.",
        "Eine unlesbare Zutatengruppe wurde weggelassen.": "Se omitió un grupo de ingredientes ilegible.",
        "Eine Zutatengruppe ohne lesbare Zutaten wurde weggelassen.": "Se omitió un grupo sin ingredientes legibles.",
        "Überzählige Zutatengruppen wurden weggelassen.": "Se omitieron los grupos de ingredientes excedentes.",
        "Ungültige Zubereitungsschritte wurden weggelassen.": "Se omitieron pasos de preparación no válidos.",
        "Ein leerer Zubereitungsschritt wurde weggelassen.": "Se omitió un paso de preparación vacío.",
        "Überzählige Zubereitungsschritte wurden weggelassen.": "Se omitieron los pasos de preparación excedentes.",
        "Eine unvollständige Ausbeute wurde als Einzelwert übernommen.": "Un rendimiento incompleto se conservó como valor único.",
        "Eine vertauschte Ausbeute-Spanne wurde korrigiert.": "Se corrigió un intervalo de rendimiento invertido.",
        "Eine zu kleine Ausbeute konnte nicht als Skalierungsbasis verwendet werden.": "Un rendimiento demasiado pequeño no pudo usarse como base de escalado.",
        "Die Ausbeute wurde auf drei Nachkommastellen gerundet.": "El rendimiento se redondeó a tres decimales.",
        "Der erkannte Dokumenttitel wurde verwendet, weil der Rezepttitel fehlte.": "Se usó el título detectado del documento porque faltaba el título de la receta.",
        "Ein zu langer Rezepttitel wurde gekürzt.": "Se acortó un título de receta demasiado largo.",
        "Eine ungültige Quellen-URL wurde weggelassen.": "Se omitió una URL de origen no válida.",
    },
}

_REPAIR_LABELS: dict[Locale, dict[str, str]] = {
    "de": {},
    "en": {
        "Vorbereitungszeit": "preparation time",
        "Garzeit": "cooking time",
        "Ruhezeit": "resting time",
        "Gesamtzeit": "total time",
        "Beschreibung": "description",
        "Notizen": "notes",
        "Quellentitel": "source title",
    },
    "zh-CN": {
        "Vorbereitungszeit": "准备时间",
        "Garzeit": "烹饪时间",
        "Ruhezeit": "静置时间",
        "Gesamtzeit": "总时间",
        "Beschreibung": "描述",
        "Notizen": "备注",
        "Quellentitel": "来源标题",
    },
    "hi": {
        "Vorbereitungszeit": "तैयारी समय",
        "Garzeit": "पकाने का समय",
        "Ruhezeit": "आराम समय",
        "Gesamtzeit": "कुल समय",
        "Beschreibung": "विवरण",
        "Notizen": "नोट्स",
        "Quellentitel": "स्रोत शीर्षक",
    },
    "es": {
        "Vorbereitungszeit": "tiempo de preparación",
        "Garzeit": "tiempo de cocción",
        "Ruhezeit": "tiempo de reposo",
        "Gesamtzeit": "tiempo total",
        "Beschreibung": "descripción",
        "Notizen": "notas",
        "Quellentitel": "título de la fuente",
    },
}


def _localized_repair_warning(locale: Locale, message: str) -> str:
    if locale == "de":
        return message
    exact = _REPAIR_WARNINGS[locale].get(message)
    if exact:
        return exact
    invalid = re.fullmatch(r"Die ungültige (.+) wurde weggelassen\.", message)
    too_long = re.fullmatch(r"Die zu lange (.+) wurde gekürzt\.", message)
    ingredient = re.fullmatch(r"Die Zutat „(.+)“ konnte nicht sicher übernommen werden\.", message)
    if invalid:
        label = _REPAIR_LABELS[locale].get(invalid.group(1), invalid.group(1))
        templates = {
            "en": "The invalid {label} was omitted.",
            "zh-CN": "已省略无效的{label}。",
            "hi": "अमान्य {label} हटा दिया गया।",
            "es": "Se omitió {label} porque no era válido.",
        }
        return templates[locale].format(label=label)
    if too_long:
        label = _REPAIR_LABELS[locale].get(too_long.group(1), too_long.group(1))
        templates = {
            "en": "The overly long {label} was shortened.",
            "zh-CN": "过长的{label}已缩短。",
            "hi": "बहुत लंबा {label} छोटा किया गया।",
            "es": "Se acortó {label} porque era demasiado largo.",
        }
        return templates[locale].format(label=label)
    if ingredient:
        name = ingredient.group(1)
        templates = {
            "en": "The ingredient “{name}” could not be retained reliably.",
            "zh-CN": "无法可靠保留食材“{name}”。",
            "hi": "सामग्री “{name}” को विश्वसनीय रूप से नहीं रखा जा सका।",
            "es": "No se pudo conservar de forma fiable el ingrediente «{name}».",
        }
        return templates[locale].format(name=name)
    return message


def _clean_string(value: Any, *, max_length: int, collapse: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()) if collapse else value.strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def _raw_text(value: Any, *, max_length: int = 500) -> str | None:
    if isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool):
        return _clean_string(str(value), max_length=max_length, collapse=True)
    return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = parse_decimal(value)
    except ValueError:
        return None
    if result is None or not result.is_finite():
        return None
    return result


def _number_range(value: Any) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Return a positive scalar/range and any trailing label-like text."""

    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = _decimal(value)
        return (number, None, None) if number is not None and number > 0 else (None, None, None)
    if not isinstance(value, str):
        return None, None, None
    text = " ".join(value.strip().split())
    if not text:
        return None, None, None
    range_match = _RANGE_PATTERN.fullmatch(text)
    if range_match:
        minimum = _decimal(range_match.group("minimum"))
        maximum = _decimal(range_match.group("maximum"))
        suffix = _clean_string(range_match.group("suffix"), max_length=80, collapse=True)
        if minimum is not None and maximum is not None and minimum > 0 and maximum > 0:
            return minimum, maximum, suffix
        return None, None, suffix
    scalar_match = _SCALAR_PATTERN.fullmatch(text)
    if scalar_match:
        number = _decimal(scalar_match.group("value"))
        suffix = _clean_string(scalar_match.group("suffix"), max_length=80, collapse=True)
        if number is not None and number > 0:
            return number, None, suffix
        return None, None, suffix
    return None, None, None


def _label_from_suffix(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip("  :;,.()[]")
    cleaned = re.sub(r"^(?:für|ergibt|yield(?:s)?|makes?)\s+", "", cleaned, flags=re.I)
    return _clean_string(cleaned, max_length=80, collapse=True)


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(slots=True)
class _RepairState:
    warnings: list[str] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    target_language: Locale = DEFAULT_LOCALE

    @classmethod
    def from_payload(
        cls,
        data: dict[str, Any],
        target_language: Locale = DEFAULT_LOCALE,
    ) -> _RepairState:
        raw_confidence = data.get("extraction_confidence")
        confidence: Literal["high", "medium", "low"] = (
            raw_confidence if raw_confidence in _CONFIDENCE_ORDER else "medium"
        )
        state = cls(confidence=confidence, target_language=target_language)
        raw_warnings = data.get("warnings")
        if isinstance(raw_warnings, list):
            for value in raw_warnings:
                warning = _clean_string(value, max_length=2000) if isinstance(value, str) else None
                if warning and warning not in state.warnings:
                    state.warnings.append(warning)
                    if len(state.warnings) == 100:
                        break
        return state

    def warn(
        self,
        message: str,
        *,
        confidence_at_most: Literal["high", "medium", "low"] = "medium",
    ) -> None:
        message = _localized_repair_warning(self.target_language, message)
        if message not in self.warnings:
            if len(self.warnings) < 100:
                self.warnings.append(message)
            else:
                self.warnings[-1] = message
        if _CONFIDENCE_ORDER[self.confidence] > _CONFIDENCE_ORDER[confidence_at_most]:
            self.confidence = confidence_at_most


def _confidence(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return -1
    return result if math.isfinite(result) else -1


def repair_category_suggestions(
    suggestions: Any,
    *,
    state: _RepairState | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(suggestions, list):
        if suggestions not in (None, []) and state is not None:
            state.warn("Ungültige Kategorie-Vorschläge wurden ignoriert.")
        return []

    valid: list[tuple[float, int, dict[str, Any]]] = []
    discarded = False
    for index, item in enumerate(suggestions):
        if not isinstance(item, dict) or not isinstance(item.get("path"), list):
            discarded = True
            continue
        path = [
            cleaned
            for part in item["path"][:12]
            if (cleaned := _clean_string(str(part), max_length=200, collapse=True)) is not None
        ]
        if not path:
            discarded = True
            continue
        confidence = _confidence(item.get("confidence"))
        if confidence < 0:
            confidence = 0
            discarded = True
        elif confidence > 1:
            confidence = 1
            discarded = True
        reason = _clean_string(item.get("reason"), max_length=1000)
        if reason is None:
            reason = translate(
                state.target_language if state else DEFAULT_LOCALE,
                "ai.automatic_suggestion",
            )
            discarded = True
        valid.append(
            (
                confidence,
                index,
                {"path": path, "confidence": confidence, "reason": reason},
            )
        )

    valid.sort(key=lambda item: (-item[0], item[1]))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for _, _, item in valid:
        key = tuple(part.casefold() for part in item["path"])
        if key in seen:
            discarded = True
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) == 20:
            discarded = discarded or len(valid) > len(unique)
            break
    if discarded and state is not None:
        state.warn("Einige ungültige oder doppelte Kategorie-Vorschläge wurden ignoriert.")
    return unique


def repair_categories_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible focused category repair used by tests and callers."""

    data["category_suggestions"] = repair_category_suggestions(data.get("category_suggestions"))
    return data


def _repair_minutes(data: dict[str, Any], state: _RepairState) -> None:
    for field_name, label in (
        ("prep_time_minutes", "Vorbereitungszeit"),
        ("cook_time_minutes", "Garzeit"),
        ("rest_time_minutes", "Ruhezeit"),
        ("total_time_minutes", "Gesamtzeit"),
    ):
        raw = data.get(field_name)
        if raw is None or raw == "":
            data[field_name] = None
            continue
        value: int | None = None
        if isinstance(raw, int) and not isinstance(raw, bool):
            value = raw
        elif isinstance(raw, float) and math.isfinite(raw) and raw.is_integer():
            value = int(raw)
        elif isinstance(raw, str):
            match = _MINUTES_PATTERN.fullmatch(raw)
            if match:
                value = int(match.group("value"))
        if value is None or not 0 <= value <= 100_000:
            data[field_name] = None
            state.warn(f"Die ungültige {label} wurde weggelassen.")
        else:
            data[field_name] = value


def _nutrition_basis(value: Any) -> str | None:
    if value in {"per_serving", "per_100g_ml"}:
        return str(value)
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    if normalized in {"pro portion", "per serving", "je portion"}:
        return "per_serving"
    if normalized in {"pro 100 g", "pro 100 ml", "per 100 g", "per 100 ml"}:
        return "per_100g_ml"
    return None


def _repair_nutrition(value: Any, state: _RepairState) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        if value not in (None, []):
            state.warn("Ungültige Nährwertangaben wurden weggelassen.")
        return []
    repaired: list[dict[str, Any]] = []
    bases: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            state.warn("Eine ungültige Nährwertzeile wurde weggelassen.")
            continue
        row = dict(raw)
        basis = _nutrition_basis(row.get("basis"))
        if basis is None or basis in bases:
            state.warn("Eine ungültige oder doppelte Nährwertzeile wurde weggelassen.")
            continue
        row["basis"] = basis
        invalid_value = False
        for field_name in _NUTRITION_FIELDS:
            raw_number = row.get(field_name)
            if raw_number in (None, ""):
                row[field_name] = None
                continue
            number = _decimal(raw_number)
            if number is None or number < 0:
                row[field_name] = None
                invalid_value = True
            else:
                row[field_name] = number
        row["note"] = _clean_string(row.get("note"), max_length=1000)
        try:
            nutrition = NutritionInput.model_validate(row)
        except ValidationError:
            state.warn("Eine unvollständige Nährwertzeile wurde weggelassen.")
            continue
        if invalid_value:
            state.warn("Ein ungültiger einzelner Nährwert wurde weggelassen.")
        repaired.append(nutrition.model_dump(mode="json"))
        bases.add(basis)
        if len(repaired) == 2:
            break
    return repaired


def _ingredient_amount_text(minimum: Any, maximum: Any) -> str | None:
    minimum_text = _raw_text(minimum, max_length=250)
    maximum_text = _raw_text(maximum, max_length=250)
    if minimum_text and maximum_text:
        return f"{minimum_text}–{maximum_text}"
    return minimum_text or maximum_text


def _append_note(note: str | None, addition: str, *, max_length: int = 1000) -> str:
    if not note:
        return addition[:max_length]
    return f"{note.rstrip()} {addition}"[:max_length]


def _repair_ingredient(raw: Any, state: _RepairState) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        state.warn("Eine unlesbare Zutat wurde weggelassen.")
        return None
    name = _clean_string(raw.get("name"), max_length=500)
    if name is None:
        state.warn("Eine Zutat ohne erkennbaren Namen wurde weggelassen.")
        return None

    item: dict[str, Any] = {
        "name": name,
        "unit": _clean_string(raw.get("unit"), max_length=80, collapse=True),
        "note": _clean_string(raw.get("note"), max_length=1000),
        "is_scalable": raw.get("is_scalable") if isinstance(raw.get("is_scalable"), bool) else True,
    }
    raw_minimum = raw.get("amount_min")
    raw_maximum = raw.get("amount_max")
    minimum: Decimal | None
    maximum: Decimal | None
    minimum, embedded_maximum, amount_suffix = _number_range(raw_minimum)
    maximum, ignored_maximum, maximum_suffix = _number_range(raw_maximum)
    del ignored_maximum
    if embedded_maximum is not None and maximum is None:
        maximum = embedded_maximum
    if item["unit"] is None:
        item["unit"] = _label_from_suffix(amount_suffix) or _label_from_suffix(maximum_suffix)

    qualitative = isinstance(raw_minimum, str) and " ".join(
        raw_minimum.strip().split()
    ).casefold().rstrip(".") in {"etwas", "einige"}
    invalid_amount = (
        not qualitative
        and (raw_minimum not in (None, "") or raw_maximum not in (None, ""))
        and minimum is None
        and maximum is None
    )
    if maximum is not None and minimum is None:
        minimum, maximum = maximum, None
        state.warn("Eine vertauschte Zutatenmenge wurde als Einzelmenge übernommen.")
    if minimum is not None and maximum is not None and maximum < minimum:
        minimum, maximum = maximum, minimum
        state.warn("Eine vertauschte Mengenspanne wurde korrigiert.")
    if invalid_amount:
        amount_text = _ingredient_amount_text(raw_minimum, raw_maximum)
        if amount_text:
            item["note"] = _append_note(
                item["note"],
                translate(
                    state.target_language,
                    "ai.amount_from_source",
                    value=amount_text,
                ),
            )
        item["is_scalable"] = False
        state.warn(
            "Eine nicht numerische Zutatenmenge wurde als Hinweis erhalten und nicht skaliert."
        )
    item["amount_min"] = raw_minimum if qualitative else minimum
    item["amount_max"] = maximum

    try:
        ingredient = IngredientInput.model_validate(item)
    except ValidationError:
        amount_text = _ingredient_amount_text(raw_minimum, raw_maximum)
        item["amount_min"] = None
        item["amount_max"] = None
        item["is_scalable"] = False
        if amount_text:
            item["note"] = _append_note(
                item["note"],
                translate(
                    state.target_language,
                    "ai.amount_from_source",
                    value=amount_text,
                ),
            )
        try:
            ingredient = IngredientInput.model_validate(item)
        except ValidationError:
            state.warn(f"Die Zutat „{name[:80]}“ konnte nicht sicher übernommen werden.")
            return None
        state.warn("Eine ungültige Zutatenmenge wurde als Hinweis erhalten und nicht skaliert.")
    return ingredient.model_dump(mode="json")


def _repair_ingredient_groups(value: Any, state: _RepairState) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        if value not in (None, []):
            state.warn("Ungültige Zutatenlisten wurden weggelassen.")
        return []
    groups: list[dict[str, Any]] = []
    for raw_group in value[:100]:
        if isinstance(raw_group, list):
            raw_group = {"ingredients": raw_group}
        if not isinstance(raw_group, dict):
            state.warn("Eine unlesbare Zutatengruppe wurde weggelassen.")
            continue
        raw_ingredients = raw_group.get("ingredients")
        if not isinstance(raw_ingredients, list):
            state.warn("Eine Zutatengruppe ohne lesbare Zutaten wurde weggelassen.")
            continue
        ingredients = [
            ingredient
            for raw_ingredient in raw_ingredients[:300]
            if (ingredient := _repair_ingredient(raw_ingredient, state)) is not None
        ]
        if not ingredients:
            continue
        groups.append(
            {
                "title": _clean_string(raw_group.get("title"), max_length=300),
                "ingredients": ingredients,
            }
        )
    if len(value) > 100:
        state.warn("Überzählige Zutatengruppen wurden weggelassen.")
    return groups


def _repair_instruction_steps(value: Any, state: _RepairState) -> list[dict[str, str]]:
    if not isinstance(value, list):
        if value not in (None, []):
            state.warn("Ungültige Zubereitungsschritte wurden weggelassen.")
        return []
    steps: list[dict[str, str]] = []
    for raw in value[:300]:
        text_value = (
            raw if isinstance(raw, str) else raw.get("text") if isinstance(raw, dict) else None
        )
        text = _clean_string(text_value, max_length=20_000)
        if text is None:
            state.warn("Ein leerer Zubereitungsschritt wurde weggelassen.")
            continue
        steps.append({"text": text})
    if len(value) > 300:
        state.warn("Überzählige Zubereitungsschritte wurden weggelassen.")
    return steps


def _repair_servings(data: dict[str, Any], state: _RepairState) -> None:
    raw_minimum = data.get("servings_min")
    raw_maximum = data.get("servings_max")
    legacy = data.get("base_servings")
    serving_text = _clean_string(data.get("serving_text"), max_length=500, collapse=True)
    serving_text_suffix: str | None = None
    if serving_text:
        _, _, serving_text_suffix = _number_range(serving_text)

    minimum, embedded_maximum, suffix = _number_range(raw_minimum)
    maximum, ignored, maximum_suffix = _number_range(raw_maximum)
    del ignored
    if minimum is None and maximum is None and raw_minimum in (None, "") and legacy is not None:
        minimum, embedded_maximum, suffix = _number_range(legacy)
        serving_text = serving_text or _raw_text(legacy)
    if minimum is None and maximum is None and serving_text:
        minimum, embedded_maximum, text_suffix = _number_range(serving_text)
        suffix = suffix or text_suffix
    if embedded_maximum is not None and maximum is None:
        maximum = embedded_maximum
    if maximum is not None and minimum is None:
        minimum, maximum = maximum, None
        state.warn("Eine unvollständige Ausbeute wurde als Einzelwert übernommen.")
    if minimum is not None and maximum is not None and maximum < minimum:
        minimum, maximum = maximum, minimum
        state.warn("Eine vertauschte Ausbeute-Spanne wurde korrigiert.")
    if minimum is not None and maximum == minimum:
        maximum = None

    label = _clean_string(data.get("serving_label"), max_length=80, collapse=True)
    label = (
        label
        or _label_from_suffix(suffix)
        or _label_from_suffix(maximum_suffix)
        or _label_from_suffix(serving_text_suffix)
    )
    if serving_text is None and minimum is not None:
        serving_text = _format_decimal(minimum)
        if maximum is not None:
            serving_text = f"{serving_text}–{_format_decimal(maximum)}"
        if label:
            serving_text = f"{serving_text} {label}"

    def bounded_float(value: Decimal | None) -> float | None:
        if value is None or value <= 0 or value > 100_000:
            return None
        rounded = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        if rounded <= 0:
            state.warn(
                "Eine zu kleine Ausbeute konnte nicht als Skalierungsbasis verwendet werden."
            )
            return None
        if rounded != value:
            state.warn("Die Ausbeute wurde auf drei Nachkommastellen gerundet.")
        return float(rounded)

    data["servings_min"] = bounded_float(minimum)
    data["servings_max"] = bounded_float(maximum)
    data["serving_label"] = label
    data["serving_text"] = serving_text


def repair_extracted_recipe_payload(
    payload: dict[str, Any],
    *,
    title_hint: str | None = None,
    target_language: Locale = DEFAULT_LOCALE,
) -> dict[str, Any]:
    """Repair optional AI fields independently before strict Pydantic validation."""

    data = copy.deepcopy(payload)
    state = _RepairState.from_payload(data, target_language)

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        fallback = _clean_string(title_hint, max_length=300)
        has_recipe_content = bool(data.get("ingredient_groups") or data.get("instruction_steps"))
        if fallback and has_recipe_content:
            data["title"] = fallback
            state.warn("Der erkannte Dokumenttitel wurde verwendet, weil der Rezepttitel fehlte.")
    elif len(title) > 300:
        data["title"] = title[:300]
        state.warn("Ein zu langer Rezepttitel wurde gekürzt.")

    for field_name, max_length, label in (
        ("description", 20_000, "Beschreibung"),
        ("notes", 50_000, "Notizen"),
        ("source_title", 500, "Quellentitel"),
    ):
        raw = data.get(field_name)
        cleaned = _clean_string(raw, max_length=max_length)
        data[field_name] = cleaned
        if raw not in (None, "") and cleaned is None:
            state.warn(f"Die ungültige {label} wurde weggelassen.")
        elif isinstance(raw, str) and len(raw.strip()) > max_length:
            state.warn(f"Die zu lange {label} wurde gekürzt.")

    raw_url = data.get("source_url")
    if raw_url in (None, ""):
        data["source_url"] = None
    else:
        try:
            data["source_url"] = str(_URL_ADAPTER.validate_python(raw_url))
        except ValidationError:
            data["source_url"] = None
            state.warn("Eine ungültige Quellen-URL wurde weggelassen.")

    _repair_minutes(data, state)
    _repair_servings(data, state)
    data["nutrition"] = _repair_nutrition(data.get("nutrition"), state)
    data["ingredient_groups"] = _repair_ingredient_groups(data.get("ingredient_groups"), state)
    data["instruction_steps"] = _repair_instruction_steps(data.get("instruction_steps"), state)
    data["category_suggestions"] = repair_category_suggestions(
        data.get("category_suggestions"), state=state
    )
    if data.get("recipe_kind") not in {"cooking", "baking"}:
        data["recipe_kind"] = infer_recipe_kind_from_categories(data["category_suggestions"])

    # Image ownership is established by the separate detection and verification
    # stages. Model-provided values here are intentionally ignored.
    data["source_regions"] = []
    data["recipe_image_candidates"] = []
    data["has_recipe_image"] = False
    data["warnings"] = state.warnings
    data["extraction_confidence"] = state.confidence
    return data


def _append_original_yield(
    notes: str | None,
    serving_text: str,
    target_language: Locale,
) -> str:
    sentence = translate(
        target_language,
        "ai.original_yield",
        value=serving_text.rstrip("."),
    )
    if notes and sentence.casefold() in notes.casefold():
        return notes
    return _append_note(notes, sentence, max_length=50_000)


def finalize_extracted_recipe(
    draft: ExtractedRecipeDraft,
    *,
    target_language: Locale = DEFAULT_LOCALE,
) -> ExtractedRecipe:
    """Map source-faithful yield semantics to the strict recipe scaling model."""

    data = draft.model_dump(mode="python", exclude={"servings_min", "servings_max", "serving_text"})
    warnings = list(draft.warnings)
    notes = draft.notes
    confidence = draft.extraction_confidence

    def add_important_warning(message: str) -> None:
        if message in warnings:
            return
        if len(warnings) == 100:
            warnings[-1] = message
        else:
            warnings.append(message)

    if draft.servings_min is None:
        base_servings = Decimal("1")
        serving_label = translate(target_language, "ai.default.recipe")
        add_important_warning(translate(target_language, "ai.warning.no_yield"))
        if confidence == "high":
            confidence = "medium"
    elif draft.servings_max is not None:
        base_servings = Decimal("1")
        serving_label = translate(target_language, "ai.default.recipe")
        serving_text = draft.serving_text or (
            f"{_format_decimal(Decimal(str(draft.servings_min)))}–"
            f"{_format_decimal(Decimal(str(draft.servings_max)))}"
            + (f" {draft.serving_label}" if draft.serving_label else "")
        )
        notes = _append_original_yield(notes, serving_text, target_language)
        add_important_warning(translate(target_language, "ai.warning.range_yield"))
        if confidence == "high":
            confidence = "medium"
    else:
        base_servings = Decimal(str(draft.servings_min))
        serving_label = draft.serving_label or translate(target_language, "ai.default.servings")

    data.update(
        {
            "base_servings": base_servings,
            "serving_label": serving_label,
            "notes": notes,
            "warnings": list(dict.fromkeys(warnings))[:100],
            "extraction_confidence": confidence,
        }
    )
    return ExtractedRecipe.model_validate(data)
