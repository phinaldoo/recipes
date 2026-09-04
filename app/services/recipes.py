from __future__ import annotations

import math
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import (
    Select,
    asc,
    delete,
    desc,
    func,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, aliased, selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.models import (
    Category,
    Ingredient,
    IngredientGroup,
    InstructionStep,
    Recipe,
    RecipeCategory,
    RecipeComment,
    RecipeImage,
    RecipeNutrition,
    RecipeOriginalAsset,
    RecipeShare,
    RecipeSource,
    RecipeTag,
    RecipeVersion,
    SearchSynonym,
    Tag,
    User,
)
from app.schemas.recipe import CategoryPathInput, RecipeInput, RecipeKind
from app.services.productivity_lock import acquire_tag_membership_lock, dialect_name


class RecipeConflict(ValueError):
    pass


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def slugify(value: str) -> str:
    pieces: list[str] = []
    for character in unicodedata.normalize("NFKC", value).casefold():
        category = unicodedata.category(character)
        if "LATIN" in unicodedata.name(character, ""):
            pieces.extend(
                item
                for item in unicodedata.normalize("NFKD", character)
                if unicodedata.category(item) != "Mn" and item.isascii() and item.isalnum()
            )
        elif category[0] in {"L", "N", "M"}:
            pieces.append(character)
        else:
            pieces.append("-")
    slug = re.sub(r"-+", "-", "".join(pieces)).strip("-")
    return slug[:240] or "rezept"


def unique_slug(db: Session, title: str, recipe_id: uuid.UUID | None = None) -> str:
    root = slugify(title)
    candidate = root
    counter = 2
    while db.scalar(
        select(Recipe.id).where(
            Recipe.slug == candidate,
            Recipe.id != recipe_id if recipe_id is not None else Recipe.id.is_not(None),
        )
    ):
        candidate = f"{root}-{counter}"
        counter += 1
    return candidate


def recipe_load_options() -> tuple[Any, ...]:
    return (
        selectinload(Recipe.created_by),
        selectinload(Recipe.updated_by),
        selectinload(Recipe.source),
        selectinload(Recipe.nutrition),
        selectinload(Recipe.ingredient_groups).selectinload(IngredientGroup.ingredients),
        selectinload(Recipe.instruction_steps),
        selectinload(Recipe.category_links)
        .selectinload(RecipeCategory.category)
        .selectinload(Category.parent, recursion_depth=19),
        selectinload(Recipe.images).selectinload(RecipeImage.asset),
        selectinload(Recipe.images).selectinload(RecipeImage.thumbnail_asset),
        selectinload(Recipe.original_assets).selectinload(RecipeOriginalAsset.asset),
        selectinload(Recipe.comments).selectinload(RecipeComment.author),
        selectinload(Recipe.tag_links).selectinload(RecipeTag.tag),
    )


def get_recipe(
    db: Session,
    recipe_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> Recipe:
    query = select(Recipe).options(*recipe_load_options()).where(Recipe.id == recipe_id)
    if not include_deleted:
        query = query.where(Recipe.deleted_at.is_(None))
    if for_update:
        query = query.with_for_update()
    recipe = db.scalar(query)
    if recipe is None:
        raise HTTPException(status_code=404, detail="Das Rezept wurde nicht gefunden.")
    return recipe


def _find_category(db: Session, name: str, parent_id: uuid.UUID | None) -> Category | None:
    conditions = [Category.normalized_name == normalize_name(name)]
    conditions.append(
        Category.parent_id == parent_id if parent_id else Category.parent_id.is_(None)
    )
    return db.scalar(select(Category).where(*conditions))


def resolve_category_path(
    db: Session, value: CategoryPathInput, *, create_missing: bool
) -> Category:
    if value.id is not None:
        category = db.get(Category, value.id)
        if category is None:
            raise ValueError("Eine ausgewählte Kategorie existiert nicht mehr")
        return category

    parent: Category | None = None
    for name in value.path:
        category = _find_category(db, name, parent.id if parent else None)
        if category is None:
            if not create_missing:
                raise ValueError(f"Die Kategorie ‚{' › '.join(value.path)}‘ existiert nicht")
            sibling_max = db.scalar(
                select(func.max(Category.position)).where(
                    Category.parent_id == parent.id if parent else Category.parent_id.is_(None)
                )
            )
            category = Category(
                parent_id=parent.id if parent else None,
                name=name.strip(),
                normalized_name=normalize_name(name),
                slug=slugify(name),
                position=(sibling_max if sibling_max is not None else -1) + 1,
                origin=value.origin,
            )
            db.add(category)
            db.flush()
        parent = category
    if parent is None:
        raise ValueError("Leerer Kategoriepfad")
    return parent


def resolve_tag(db: Session, name: str) -> Tag:
    acquire_tag_membership_lock(db)
    normalized = normalize_name(name)
    if dialect_name(db) == "postgresql":
        db.execute(
            postgresql_insert(Tag)
            .values(
                id=uuid.uuid4(),
                name=" ".join(name.strip().split()),
                normalized_name=normalized,
            )
            .on_conflict_do_nothing(index_elements=[Tag.normalized_name])
        )
        tag = db.scalar(select(Tag).where(Tag.normalized_name == normalized))
        if tag is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("Das Schlagwort konnte nicht gelesen werden")
        return tag
    tag = db.scalar(select(Tag).where(Tag.normalized_name == normalized))
    if tag is None:
        tag = Tag(name=" ".join(name.strip().split()), normalized_name=normalized)
        db.add(tag)
        db.flush()
    return tag


def recipe_snapshot(recipe: Recipe) -> dict[str, object]:
    return {
        "title": recipe.title,
        "description": recipe.description,
        "recipe_kind": recipe.recipe_kind,
        "base_servings": str(recipe.base_servings),
        "serving_label": recipe.serving_label,
        "prep_time_minutes": recipe.prep_time_minutes,
        "cook_time_minutes": recipe.cook_time_minutes,
        "rest_time_minutes": recipe.rest_time_minutes,
        "total_time_minutes": recipe.total_time_minutes,
        "total_time_is_manual": recipe.total_time_is_manual,
        "nutrition": [
            {
                "basis": value.basis,
                "energy_kj": str(value.energy_kj) if value.energy_kj is not None else None,
                "energy_kcal": str(value.energy_kcal) if value.energy_kcal is not None else None,
                "fat_g": str(value.fat_g) if value.fat_g is not None else None,
                "saturated_fat_g": str(value.saturated_fat_g)
                if value.saturated_fat_g is not None
                else None,
                "carbohydrates_g": str(value.carbohydrates_g)
                if value.carbohydrates_g is not None
                else None,
                "sugars_g": str(value.sugars_g) if value.sugars_g is not None else None,
                "fiber_g": str(value.fiber_g) if value.fiber_g is not None else None,
                "protein_g": str(value.protein_g) if value.protein_g is not None else None,
                "salt_g": str(value.salt_g) if value.salt_g is not None else None,
                "note": value.note,
            }
            for value in recipe.nutrition
        ],
        "notes": recipe.notes,
        "status": recipe.status,
        "ingredient_groups": [
            {
                "title": group.title,
                "ingredients": [
                    {
                        "amount_min": str(item.amount_min) if item.amount_min is not None else None,
                        "amount_max": str(item.amount_max) if item.amount_max is not None else None,
                        "unit": item.unit,
                        "name": item.name,
                        "note": item.note,
                        "is_scalable": item.is_scalable,
                    }
                    for item in group.ingredients
                ],
            }
            for group in recipe.ingredient_groups
        ],
        "instruction_steps": [{"text": step.text} for step in recipe.instruction_steps],
        "categories": [
            {
                "id": str(category.id),
                "path": category.path.split(" › "),
                "origin": category.origin,
            }
            for category in recipe.categories
        ],
        "tags": [tag.name for tag in recipe.tags],
        "source": (
            {"title": recipe.source.title, "url": recipe.source.url} if recipe.source else None
        ),
    }


def create_version(db: Session, recipe: Recipe, user: User, summary: str) -> None:
    last = db.scalar(
        select(func.max(RecipeVersion.version_number)).where(RecipeVersion.recipe_id == recipe.id)
    )
    version_number = (last or 0) + 1
    db.add(
        RecipeVersion(
            recipe_id=recipe.id,
            changed_by_user_id=user.id,
            version_number=version_number,
            snapshot=recipe_snapshot(recipe),
            change_summary=summary,
        )
    )
    retention = get_settings().recipe_version_retention
    oldest_to_keep = version_number - retention + 1
    if oldest_to_keep > 1:
        db.execute(
            delete(RecipeVersion).where(
                RecipeVersion.recipe_id == recipe.id,
                RecipeVersion.version_number < oldest_to_keep,
            )
        )


def apply_recipe_input(
    db: Session,
    recipe: Recipe,
    payload: RecipeInput,
    user: User,
    *,
    create_missing_categories: bool = True,
) -> Recipe:
    if payload.expected_updated_at and recipe.updated_at:
        actual = recipe.updated_at.astimezone(UTC)
        expected = payload.expected_updated_at.astimezone(UTC)
        if actual != expected:
            raise RecipeConflict(
                "Das Rezept wurde inzwischen von einer anderen Person geändert. "
                "Bitte lade es neu und übernimm deine Änderungen erneut."
            )

    was_active = recipe.status == "active"
    recipe.title = payload.title
    recipe.slug = unique_slug(db, payload.title, recipe.id)
    recipe.description = payload.description
    recipe.recipe_kind = payload.recipe_kind
    recipe.base_servings = payload.base_servings
    recipe.serving_label = payload.serving_label
    recipe.prep_time_minutes = payload.prep_time_minutes
    recipe.cook_time_minutes = payload.cook_time_minutes
    recipe.rest_time_minutes = payload.rest_time_minutes
    recipe.total_time_minutes = payload.total_time_minutes
    recipe.total_time_is_manual = payload.total_time_is_manual
    recipe.notes = payload.notes
    recipe.status = payload.status
    if was_active and payload.status != "active":
        db.execute(
            update(RecipeShare)
            .where(RecipeShare.recipe_id == recipe.id, RecipeShare.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
    recipe.updated_by_user_id = user.id
    recipe.updated_by_name_snapshot = user.visible_name
    recipe.updated_at = datetime.now(UTC)

    # Remove position/category rows in a separate flush. PostgreSQL checks the
    # unique keys before orphan deletes if old and replacement rows share one flush.
    recipe.ingredient_groups.clear()
    recipe.instruction_steps.clear()
    recipe.nutrition.clear()
    recipe.category_links.clear()
    recipe.tag_links.clear()
    db.flush()

    for group_position, group_payload in enumerate(payload.ingredient_groups):
        group = IngredientGroup(title=group_payload.title, position=group_position)
        for item_position, item_payload in enumerate(group_payload.ingredients):
            group.ingredients.append(
                Ingredient(
                    amount_min=item_payload.amount_min,
                    amount_max=item_payload.amount_max,
                    unit=item_payload.unit,
                    name=item_payload.name,
                    note=item_payload.note,
                    is_scalable=item_payload.is_scalable,
                    position=item_position,
                )
            )
        recipe.ingredient_groups.append(group)

    for position, step in enumerate(payload.instruction_steps):
        recipe.instruction_steps.append(InstructionStep(position=position, text=step.text))

    for value in payload.nutrition:
        recipe.nutrition.append(
            RecipeNutrition(
                basis=value.basis,
                energy_kj=value.energy_kj,
                energy_kcal=value.energy_kcal,
                fat_g=value.fat_g,
                saturated_fat_g=value.saturated_fat_g,
                carbohydrates_g=value.carbohydrates_g,
                sugars_g=value.sugars_g,
                fiber_g=value.fiber_g,
                protein_g=value.protein_g,
                salt_g=value.salt_g,
                note=value.note,
            )
        )

    categories = [
        resolve_category_path(db, value, create_missing=create_missing_categories)
        for value in payload.categories
    ]
    category_ids = [category.id for category in categories]
    if len(set(category_ids)) != len(category_ids):
        raise ValueError("Eine Kategorie darf nur einmal ausgewählt werden")
    if len(category_ids) > 20:
        raise ValueError("Ein Rezept darf höchstens 20 Kategorien haben")
    recipe.category_links.extend(RecipeCategory(category=category) for category in categories)
    tags = [resolve_tag(db, name) for name in payload.tags]
    recipe.tag_links.extend(RecipeTag(tag=tag) for tag in tags)

    if payload.source and (payload.source.title or payload.source.url):
        if recipe.source is None:
            recipe.source = RecipeSource()
        recipe.source.title = payload.source.title
        recipe.source.url = str(payload.source.url) if payload.source.url else None
    elif recipe.source is not None:
        recipe.source = None

    db.flush()
    refresh_search_document(db, recipe)
    return recipe


def create_recipe(db: Session, payload: RecipeInput, user: User) -> Recipe:
    recipe = Recipe(
        created_by_user_id=user.id,
        updated_by_user_id=user.id,
        created_by_name_snapshot=user.visible_name,
        updated_by_name_snapshot=user.visible_name,
        title=payload.title,
        slug=unique_slug(db, payload.title),
        recipe_kind=payload.recipe_kind,
        base_servings=payload.base_servings,
        serving_label=payload.serving_label,
    )
    db.add(recipe)
    db.flush()
    apply_recipe_input(db, recipe, payload, user)
    create_version(db, recipe, user, "Rezept erstellt")
    return recipe


def update_recipe(db: Session, recipe: Recipe, payload: RecipeInput, user: User) -> Recipe:
    updated = apply_recipe_input(db, recipe, payload, user)
    create_version(db, updated, user, "Rezept geändert")
    db.flush()
    return updated


def soft_delete_recipe(db: Session, recipe: Recipe, user: User) -> None:
    create_version(db, recipe, user, "In den Papierkorb verschoben")
    recipe.deleted_at = datetime.now(UTC)
    recipe.updated_by_user_id = user.id
    recipe.updated_by_name_snapshot = user.visible_name
    recipe.updated_at = datetime.now(UTC)
    db.execute(
        update(RecipeShare)
        .where(RecipeShare.recipe_id == recipe.id, RecipeShare.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.flush()


def restore_recipe(db: Session, recipe: Recipe, user: User) -> None:
    recipe.deleted_at = None
    recipe.updated_by_user_id = user.id
    recipe.updated_by_name_snapshot = user.visible_name
    recipe.updated_at = datetime.now(UTC)
    create_version(db, recipe, user, "Aus dem Papierkorb wiederhergestellt")
    db.flush()


def build_search_document(recipe: Recipe) -> str:
    parts: list[str] = [recipe.title]
    for value in (recipe.description, recipe.notes):
        if value:
            parts.append(value)
    parts.extend(category.path for category in recipe.categories)
    parts.extend(tag.name for tag in recipe.tags)
    for group in recipe.ingredient_groups:
        if group.title:
            parts.append(group.title)
        for item in group.ingredients:
            parts.extend(value for value in (item.name, item.unit, item.note) if value)
    parts.extend(step.text for step in recipe.instruction_steps)
    for nutrition in recipe.nutrition:
        parts.extend(_nutrition_search_parts(nutrition))
    if recipe.source:
        parts.extend(value for value in (recipe.source.title, recipe.source.url) if value)
    for comment in recipe.comments:
        if comment.deleted_at is None:
            parts.extend((comment.author_name_snapshot, comment.text))
    return "\n".join(parts)


def _weighted_search_sections(recipe: Recipe) -> tuple[str, str, str, str]:
    high = [category.path for category in recipe.categories]
    high.extend(tag.name for tag in recipe.tags)
    normal: list[str] = []
    for group in recipe.ingredient_groups:
        if group.title:
            high.append(group.title)
        for item in group.ingredients:
            high.extend(value for value in (item.name, item.unit) if value)
            if item.note:
                normal.append(item.note)
    medium = [value for value in (recipe.description,) if value]
    if recipe.source:
        medium.extend(value for value in (recipe.source.title, recipe.source.url) if value)
    normal.extend(step.text for step in recipe.instruction_steps)
    for value in recipe.nutrition:
        normal.extend(_nutrition_search_parts(value))
    if recipe.notes:
        normal.append(recipe.notes)
    for comment in recipe.comments:
        if comment.deleted_at is None:
            normal.extend((comment.author_name_snapshot, comment.text))
    return recipe.title, "\n".join(high), "\n".join(medium), "\n".join(normal)


def _nutrition_search_parts(value: RecipeNutrition) -> list[str]:
    basis = (
        "pro Portion per serving por porción प्रति सर्विंग 每份"
        if value.basis == "per_serving"
        else "pro 100 g ml per 100 g ml por 100 g ml प्रति 100 ग्राम मिली 每100克毫升"
    )
    result = ["Nährwerte nutrition nutrición पोषण 营养 Brennwerte energy energía ऊर्जा 能量", basis]
    fields = (
        (value.energy_kj, "kJ Energie energy energía ऊर्जा 能量"),
        (value.energy_kcal, "kcal Kalorien calories calorías कैलोरी 卡路里 Energie energy"),
        (value.fat_g, "g Fett fat grasas वसा 脂肪"),
        (value.saturated_fat_g, "g gesättigte Fettsäuren saturated saturadas संतृप्त 饱和脂肪"),
        (value.carbohydrates_g, "g Kohlenhydrate carbohydrates hidratos कार्बोहाइड्रेट 碳水化合物"),
        (value.sugars_g, "g Zucker sugar azúcar शर्करा 糖"),
        (value.fiber_g, "g Ballaststoffe fibre fiber fibra फ़ाइबर 膳食纤维"),
        (value.protein_g, "g Eiweiß Protein proteínas प्रोटीन 蛋白质"),
        (value.salt_g, "g Salz salt sal नमक 盐"),
    )
    result.extend(f"{amount} {label}" for amount, label in fields if amount is not None)
    if value.note:
        result.append(value.note)
    return result


def refresh_search_document(db: Session, recipe: Recipe) -> None:
    document = build_search_document(recipe)
    title, high, medium, normal = _weighted_search_sections(recipe)
    vector = (
        func.setweight(func.to_tsvector("simple", func.unaccent(title)), literal_column("'A'"))
        .op("||")(
            func.setweight(func.to_tsvector("simple", func.unaccent(high)), literal_column("'B'"))
        )
        .op("||")(
            func.setweight(func.to_tsvector("simple", func.unaccent(medium)), literal_column("'C'"))
        )
        .op("||")(
            func.setweight(func.to_tsvector("simple", func.unaccent(normal)), literal_column("'D'"))
        )
    )

    # ``TimestampMixin.updated_at`` has a client-side ``onupdate=now()`` default.
    # Updating the derived search fields through the mapped Recipe entity would
    # therefore replace the recipe's semantic modification timestamp. On
    # PostgreSQL, ``now()`` is fixed for the whole transaction, so an edit made in
    # the same transaction as creation could even appear to have no newer version.
    # It would also destroy a historical timestamp restored from a JSON package.
    #
    # Use one explicit DML statement and pass the timestamp so search-index
    # maintenance is timestamp-neutral. Keep the identity-map state in sync
    # without scheduling a second ORM UPDATE.
    db.execute(
        update(Recipe)
        .where(Recipe.id == recipe.id)
        .values(
            search_document=document,
            search_vector=vector,
            updated_at=recipe.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    set_committed_value(recipe, "search_document", document)
    db.expire(recipe, ["search_vector"])


MAX_SEARCH_QUERY_VARIANTS = 64
MAX_SEARCH_QUERY_LENGTH = 4096
MAX_SEARCH_EXPANSION_LENGTH = 32768


def expand_search_queries(db: Session, value: str) -> list[str]:
    original = value.strip()
    if not original:
        return []
    if len(original) > MAX_SEARCH_QUERY_LENGTH:
        raise HTTPException(status_code=422, detail="Die Suchanfrage ist zu lang.")
    replacements: list[tuple[str, str]] = []
    for synonym in db.scalars(select(SearchSynonym)):
        replacements.extend(((synonym.term, synonym.synonym), (synonym.synonym, synonym.term)))

    unique = {normalize_name(original): original}
    frontier = [original]
    total_length = len(original)
    while frontier and len(unique) < MAX_SEARCH_QUERY_VARIANTS:
        query = frontier.pop(0)
        for source, replacement in replacements:
            pattern = rf"(?<!\w){re.escape(source)}(?!\w)"
            # Measure before substituting: recursive rules can double the text
            # at every step, long before the variant-count limit is reached.
            expanded_length = len(query)
            for match in re.finditer(pattern, query, flags=re.IGNORECASE):
                expanded_length += len(replacement) - (match.end() - match.start())
                if expanded_length > MAX_SEARCH_QUERY_LENGTH:
                    raise HTTPException(
                        status_code=422, detail="Die Suchsynonyme erzeugen eine zu große Anfrage."
                    )
            replacement_template = replacement.replace("\\", "\\\\")
            replaced = re.sub(pattern, replacement_template, query, flags=re.IGNORECASE)
            normalized = normalize_name(replaced)
            if replaced == query or normalized in unique:
                continue
            total_length += max(len(replaced), len(normalized))
            if total_length > MAX_SEARCH_EXPANSION_LENGTH:
                raise HTTPException(
                    status_code=422, detail="Die Suchsynonyme erzeugen eine zu große Anfrage."
                )
            unique[normalized] = replaced
            frontier.append(replaced)
            if len(unique) >= MAX_SEARCH_QUERY_VARIANTS:
                break
    return list(unique.values())


def escape_ilike(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_recipes(
    db: Session,
    *,
    q: str = "",
    category_ids: list[uuid.UUID] | None = None,
    recipe_kind: RecipeKind | None = None,
    sort: str = "updated_desc",
    page: int = 1,
    page_size: int = 24,
    include_deleted: bool = False,
    only_deleted: bool = False,
) -> tuple[list[Recipe], int, int, int]:
    """List recipes, treating each category filter as an inclusive subtree.

    Multiple selected category subtrees are combined with AND semantics.
    """
    if only_deleted:
        base: Select[tuple[Recipe]] = select(Recipe).where(Recipe.deleted_at.is_not(None))
    elif include_deleted:
        base = select(Recipe)
    else:
        base = select(Recipe).where(Recipe.deleted_at.is_(None))
    base = base.where(Recipe.status == "active")
    if recipe_kind is not None:
        base = base.where(Recipe.recipe_kind == recipe_kind)

    rank = None
    clean_q = q.strip()
    if clean_q:
        expanded = expand_search_queries(db, clean_q)
        ts_queries = [
            func.websearch_to_tsquery("simple", func.unaccent(candidate)) for candidate in expanded
        ]
        ranks = [func.ts_rank_cd(Recipe.search_vector, query) for query in ts_queries]
        rank = ranks[0] if len(ranks) == 1 else func.greatest(*ranks)
        conditions: list[Any] = [Recipe.search_vector.op("@@")(query) for query in ts_queries]
        conditions.extend(
            Recipe.search_document.ilike(f"%{escape_ilike(candidate)}%", escape="\\")
            for candidate in expanded
        )
        conditions.extend(
            func.unaccent(Recipe.search_document).ilike(
                func.unaccent(f"%{escape_ilike(candidate)}%"), escape="\\"
            )
            for candidate in expanded
        )
        conditions.append(func.similarity(Recipe.title, clean_q) > Decimal("0.12"))
        base = base.where(or_(*conditions))

    selected_category_ids = list(dict.fromkeys(category_ids or []))
    if selected_category_ids:
        category_subtree = (
            select(
                Category.id.label("category_id"),
                Category.id.label("selected_category_id"),
            )
            .where(Category.id.in_(selected_category_ids))
            .cte("selected_category_subtree", recursive=True)
        )
        child_category = aliased(Category)
        # UNION (rather than UNION ALL) also makes this defensive against a
        # legacy category cycle: each category/root pair is visited only once.
        category_subtree = category_subtree.union(
            select(child_category.id, category_subtree.c.selected_category_id).join(
                category_subtree,
                child_category.parent_id == category_subtree.c.category_id,
            )
        )

        for category_id in selected_category_ids:
            base = base.where(
                select(RecipeCategory.recipe_id)
                .join(
                    category_subtree,
                    RecipeCategory.category_id == category_subtree.c.category_id,
                )
                .where(
                    RecipeCategory.recipe_id == Recipe.id,
                    category_subtree.c.selected_category_id == category_id,
                )
                .exists()
            )

    count_query = select(func.count()).select_from(base.order_by(None).subquery())
    total = int(db.scalar(count_query) or 0)
    pages = max(1, math.ceil(total / page_size))
    page = min(max(page, 1), pages)

    if clean_q and rank is not None:
        base = base.order_by(desc(rank), asc(Recipe.title), asc(Recipe.id))
    elif sort == "title_asc":
        base = base.order_by(asc(func.lower(Recipe.title)), asc(Recipe.id))
    elif sort == "created_desc":
        base = base.order_by(desc(Recipe.created_at), desc(Recipe.id))
    else:
        base = base.order_by(desc(Recipe.updated_at), desc(Recipe.id))

    recipes = list(
        db.scalars(
            base.options(
                selectinload(Recipe.category_links).selectinload(RecipeCategory.category),
                selectinload(Recipe.images),
                selectinload(Recipe.comments),
                selectinload(Recipe.tag_links).selectinload(RecipeTag.tag),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).unique()
    )
    return recipes, total, pages, page
