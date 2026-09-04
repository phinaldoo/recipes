from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.productivity import api_router, page_router
from app.database import Base
from app.schemas.productivity import (
    SynonymPayload,
    TagPayload,
    VersionRestoreInput,
)
from app.schemas.recipe import RecipeInput
from app.services import recipes as recipe_service
from app.services.productivity import (
    favorite_recipe_ids,
    snapshot_diff,
    version_history,
)
from app.services.recipes import escape_ilike, expand_search_queries
from app.services.shares import share_token_hash


def test_recipe_tags_are_cleaned_and_deduplicated_case_insensitively() -> None:
    payload = RecipeInput(
        title="Suppe",
        base_servings="4",
        tags=["  Schnell  ", "schnell", "  vegan   einfach  ", ""],
    )

    assert payload.tags == ["Schnell", "vegan einfach"]


def test_recipe_tags_are_nfkc_normalized_before_deduplication() -> None:
    payload = RecipeInput(
        title="Suppe",
        base_servings="4",
        tags=["Café", "Cafe\u0301", "Ｖｅｇａｎ", "vegan"],
    )

    assert payload.tags == ["Café", "Vegan"]


def test_synonym_pair_must_contain_two_distinct_terms() -> None:
    with pytest.raises(ValidationError):
        SynonymPayload(term="Kartoffel", synonym="kartoffel")


@pytest.mark.parametrize(
    ("payload", "repetitions"),
    [
        (lambda value: TagPayload(name=value), 100),
        (lambda value: SynonymPayload(term=value, synonym="anders"), 100),
    ],
)
def test_nfkc_expansion_cannot_exceed_database_column_lengths(
    payload: Callable[[str], object], repetitions: int
) -> None:
    with pytest.raises(ValidationError):
        payload("ﬃ" * repetitions)


def test_version_restore_requires_timezone_aware_datetimes() -> None:
    with pytest.raises(ValidationError):
        VersionRestoreInput(expected_updated_at=datetime(2026, 8, 31, 12))


def test_shopping_and_meal_planning_are_not_exposed_or_mapped() -> None:
    api_paths = {route.path for route in api_router.routes}
    page_paths = {route.path for route in page_router.routes}

    assert not any(path.startswith("/shopping-list") for path in api_paths)
    assert not any(path.startswith("/meal-plan") for path in api_paths)
    assert "/einkaufsliste" not in page_paths
    assert "/wochenplan" not in page_paths
    assert "shopping_list_items" not in Base.metadata.tables
    assert "meal_plan_entries" not in Base.metadata.tables


def test_favorite_recipe_ids_is_user_scoped_and_skips_empty_lookups() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    requested_ids = [uuid.uuid4(), uuid.uuid4()]
    db = Mock()
    db.scalars.return_value = [requested_ids[1]]

    assert favorite_recipe_ids(db, user, requested_ids) == {requested_ids[1]}

    statement = db.scalars.call_args.args[0]
    parameters = statement.compile().params.values()
    assert user.id in parameters
    assert any(
        isinstance(value, (list, tuple)) and set(value) == set(requested_ids)
        for value in parameters
    )

    db.reset_mock()
    assert favorite_recipe_ids(db, user, []) == set()
    db.scalars.assert_not_called()


def test_search_synonyms_expand_all_combinations() -> None:
    db = SimpleNamespace(
        scalars=lambda _statement: [
            SimpleNamespace(term="Hähnchen", synonym="Poulet"),
            SimpleNamespace(term="Kartoffeln", synonym="Erdäpfel"),
        ]
    )

    expanded = expand_search_queries(db, "Hähnchen Kartoffeln")  # type: ignore[arg-type]

    assert "Poulet Erdäpfel" in expanded
    assert {"Hähnchen Kartoffeln", "Poulet Kartoffeln", "Hähnchen Erdäpfel"} <= set(expanded)


@pytest.mark.parametrize(
    "pairs",
    [[("a", "a a")], [("a", "b b"), ("b", "a a")], [("a", "A A")]],
)
def test_recursive_synonym_growth_is_rejected_before_large_substitution(
    monkeypatch: pytest.MonkeyPatch, pairs: list[tuple[str, str]]
) -> None:
    db = SimpleNamespace(
        scalars=lambda _statement: [SimpleNamespace(term=a, synonym=b) for a, b in pairs]
    )
    substitute = recipe_service.re.sub
    lengths: list[int] = []

    def checked_sub(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = substitute(*args, **kwargs)
        lengths.append(len(result))
        assert len(result) <= recipe_service.MAX_SEARCH_QUERY_LENGTH
        return result

    monkeypatch.setattr(recipe_service.re, "sub", checked_sub)
    with pytest.raises(HTTPException) as caught:
        expand_search_queries(db, "a")  # type: ignore[arg-type]
    assert caught.value.status_code == 422
    assert lengths


def test_synonym_expansion_has_an_aggregate_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace(
        scalars=lambda _statement: [
            SimpleNamespace(term="a", synonym="b"),
            SimpleNamespace(term="b", synonym="c"),
        ]
    )
    monkeypatch.setattr(recipe_service, "MAX_SEARCH_EXPANSION_LENGTH", 2)
    with pytest.raises(HTTPException) as caught:
        expand_search_queries(db, "a")  # type: ignore[arg-type]
    assert caught.value.status_code == 422


def test_synonyms_preserve_literal_replacements_and_word_boundaries() -> None:
    db = SimpleNamespace(scalars=lambda _statement: [SimpleNamespace(term="Mehl", synonym=r"a\1")])
    assert expand_search_queries(db, "MEHL Mehlig") == ["MEHL Mehlig", r"a\1 Mehlig"]  # type: ignore[arg-type]


def test_ilike_fallback_escapes_wildcards_and_escape_character() -> None:
    assert escape_ilike(r"50%_schnell\einfach") == r"50\%\_schnell\\einfach"


def test_version_diff_has_german_labels_and_omits_unchanged_values() -> None:
    changes = snapshot_diff(
        {"title": "Alt", "description": "Gleich", "tags": ["Schnell"]},
        {"title": "Neu", "description": "Gleich", "tags": ["Vegan"]},
    )

    assert changes == [
        {"field": "Titel", "before": "Alt", "after": "Neu"},
        {"field": "Schlagwörter", "before": "Schnell", "after": "Vegan"},
    ]


def test_version_diff_describes_nested_content_changes() -> None:
    changes = snapshot_diff(
        {
            "ingredient_groups": [
                {
                    "title": "Teig",
                    "ingredients": [{"amount_min": "1", "unit": "kg", "name": "Mehl"}],
                }
            ],
            "instruction_steps": [{"text": "Kurz kneten"}],
            "categories": [{"path": ["Backen", "Brot"]}],
        },
        {
            "ingredient_groups": [
                {
                    "title": "Teig",
                    "ingredients": [{"amount_min": "2", "unit": "kg", "name": "Mehl"}],
                }
            ],
            "instruction_steps": [{"text": "Zehn Minuten kneten"}],
            "categories": [{"path": ["Backen", "Sauerteig"]}],
        },
    )

    assert changes == [
        {"field": "Zutaten", "before": "Teig: 1 kg Mehl", "after": "Teig: 2 kg Mehl"},
        {
            "field": "Zubereitung",
            "before": "1. Kurz kneten",
            "after": "1. Zehn Minuten kneten",
        },
        {
            "field": "Kategorien",
            "before": "Backen › Brot",
            "after": "Backen › Sauerteig",
        },
    ]


def test_version_diff_summarizes_structured_nutrition() -> None:
    changes = snapshot_diff(
        {"nutrition": []},
        {
            "nutrition": [
                {
                    "basis": "per_serving",
                    "energy_kcal": "347",
                    "protein_g": "10",
                    "note": "Eine Portion entspricht einem Viertel.",
                }
            ]
        },
    )

    assert changes == [
        {
            "field": "Nährwerte",
            "before": "–",
            "after": ("pro Portion: 347 kcal, 10 g Eiweiß, Eine Portion entspricht einem Viertel."),
        }
    ]


def test_version_history_paginates_and_uses_preceding_page_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe_id = uuid.uuid4()
    versions = [
        SimpleNamespace(
            version_number=number,
            snapshot={"title": f"Stand {number}"},
        )
        for number in range(50, 25, -1)
    ]
    db = Mock()
    db.scalar.side_effect = [51, {"title": "Stand 25"}]
    db.scalars.return_value = versions
    monkeypatch.setattr("app.services.productivity.get_recipe", lambda *_args, **_kwargs: object())

    history, total, pages, page = version_history(db, recipe_id, page=2, page_size=25)

    assert (total, pages, page) == (51, 3, 2)
    assert history[0][0].version_number == 50
    assert history[-1] == (
        versions[-1],
        [{"field": "Titel", "before": "Stand 25", "after": "Stand 26"}],
    )


def test_share_token_hash_is_deterministic_without_storing_the_token() -> None:
    token = "a" * 43

    assert share_token_hash(token) == share_token_hash(token)
    assert share_token_hash(token) != token
    assert len(share_token_hash(token)) == 64
