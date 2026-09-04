from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.i18n import DEFAULT_LOCALE, Locale, format_decimal_locale, format_duration_locale

COMMON_FRACTIONS = {
    Decimal("0.25"): "¼",
    Decimal("0.333"): "⅓",
    Decimal("0.5"): "½",
    Decimal("0.667"): "⅔",
    Decimal("0.75"): "¾",
}


def scale_amount(
    amount: Decimal | None,
    *,
    base_servings: Decimal,
    desired_servings: Decimal,
    scalable: bool = True,
) -> Decimal | None:
    if amount is None or not scalable:
        return amount
    if base_servings <= 0 or desired_servings <= 0:
        raise ValueError("Portionsangaben müssen größer als null sein")
    return (
        (amount * desired_servings / base_servings)
        .quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        .normalize()
    )


def format_amount(value: Decimal | None, locale: Locale = DEFAULT_LOCALE) -> str:
    if value is None:
        return ""
    integer = int(value)
    remainder = (value - integer).quantize(Decimal("0.001"))
    fraction = COMMON_FRACTIONS.get(remainder)
    if fraction:
        return f"{integer if integer else ''}{fraction}"
    return format_decimal_locale(value, locale)


def format_decimal(value: Decimal | None, locale: Locale = DEFAULT_LOCALE) -> str:
    return format_decimal_locale(value, locale)


def format_duration(minutes: int | None, locale: Locale = DEFAULT_LOCALE) -> str:
    return format_duration_locale(minutes, locale)
