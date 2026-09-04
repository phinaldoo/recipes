from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# Productive actions such as renaming tags and toggling favourites use
# read-before-write logic. A transaction-scoped lock keeps those operations
# deterministic across API workers without persisting a mutable lock table.
PRODUCTIVITY_LOCK_ID = 6_812_044_719_221
TAG_MEMBERSHIP_LOCK_ID = 6_812_044_719_222


def dialect_name(db: Session) -> str | None:
    get_bind = getattr(db, "get_bind", None)
    if get_bind is None:
        return None
    return str(get_bind().dialect.name)


def _acquire_transaction_lock(db: Session, lock_id: int) -> None:
    dialect = dialect_name(db)
    if dialect == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )
    elif dialect not in {None, "sqlite"}:
        raise RuntimeError("Produktivitätsaktionen benötigen PostgreSQL-Transaktionssperren")


def acquire_productivity_lock(db: Session) -> None:
    _acquire_transaction_lock(db, PRODUCTIVITY_LOCK_ID)


def acquire_tag_membership_lock(db: Session) -> None:
    """Serialize tag deletion with recipe/tag resolution.

    This lock is deliberately separate from the broader productivity lock. Recipe
    updates already hold their recipe row before resolving tags; deletion never
    takes a recipe-row lock, so this ordering cannot form a lock cycle.
    """

    _acquire_transaction_lock(db, TAG_MEMBERSHIP_LOCK_ID)
