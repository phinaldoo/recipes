from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.models import MediaAsset, RecipeShare
from app.services.shares import (
    PUBLIC_RECIPE_LOAD_OPTIONS,
    create_share,
    resolve_share_image,
)


def test_public_recipe_template_shows_flattened_category_names() -> None:
    project_root = Path(__file__).resolve().parents[1]
    template = (project_root / "app/templates/productivity/public-share.html").read_text(
        encoding="utf-8"
    )

    assert "for category in recipe.expanded_categories" in template
    assert "{{ category.name }}" in template
    assert "{{ category.path }}" not in template


def test_share_page_reuses_the_styled_recipe_sort_select() -> None:
    project_root = Path(__file__).resolve().parents[1]
    template = (project_root / "app/templates/productivity/shares.html").read_text(encoding="utf-8")

    assert 'class="sort-select share-duration-select"' in template
    assert 'class="share-layout"' in template
    assert 'data-share-count aria-live="polite"' in template


def test_public_recipe_loader_whitelists_only_rendered_relationships() -> None:
    paths = {str(option.path) for option in PUBLIC_RECIPE_LOAD_OPTIONS}

    assert any("Recipe(recipes)" in path and "relationship:*" in path for path in paths)
    assert any("Recipe.source" in path for path in paths)
    assert any("Recipe.nutrition" in path for path in paths)
    assert any("IngredientGroup.ingredients" in path for path in paths)
    assert any("Recipe.instruction_steps" in path for path in paths)
    assert any("RecipeCategory.category" in path for path in paths)
    assert any("Recipe.images" in path for path in paths)
    assert any("RecipeTag.tag" in path for path in paths)
    assert not any("Recipe.comments" in path for path in paths)
    assert not any("Recipe.original_assets" in path for path in paths)
    assert not any("Recipe.created_by" in path or "Recipe.updated_by" in path for path in paths)


def test_share_creation_locks_only_the_recipe_row() -> None:
    recipe_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = Mock()
    db.scalar.return_value = SimpleNamespace(id=recipe_id, status="active")

    share, token = create_share(
        db,
        SimpleNamespace(id=user_id),  # type: ignore[arg-type]
        recipe_id,
        expires_in_days=30,
    )

    statement = db.scalar.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FROM recipes" in sql
    assert "FOR UPDATE" in sql
    assert "JOIN" not in sql
    assert share.recipe_id == recipe_id
    assert share.created_by_user_id == user_id
    assert len(token) >= 32


def test_public_image_resolution_is_one_scoped_query_without_recipe_graph() -> None:
    share = RecipeShare(
        recipe_id=uuid.uuid4(),
        token_hash="a" * 64,
        token_prefix="prefix",
    )
    asset = MediaAsset(
        kind="recipe_image",
        storage_key="media/example.jpg",
        original_filename="example.jpg",
        mime_type="image/jpeg",
        byte_size=123,
        sha256="b" * 64,
    )
    db = Mock()
    db.execute.return_value.one_or_none.return_value = (share, asset)

    resolved_share, resolved_asset = resolve_share_image(db, "x" * 43, uuid.uuid4())

    assert (resolved_share, resolved_asset) == (share, asset)
    statement = db.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "JOIN recipes" in sql
    assert "JOIN recipe_images" in sql
    assert "JOIN media_assets" in sql
    assert "recipe_comments" not in sql
    assert "recipe_original_assets" not in sql
    db.flush.assert_called_once_with()


def test_public_image_rejects_malformed_token_before_database_access() -> None:
    db = Mock()

    with pytest.raises(HTTPException) as error:
        resolve_share_image(db, "short", uuid.uuid4())

    assert error.value.status_code == 404
    db.execute.assert_not_called()
