from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import engine

# Stable, application-specific PostgreSQL advisory-lock identifier. Every normal
# request/worker takes the shared variant while a restore takes the exclusive
# variant.  The lock is session scoped intentionally: application code commits
# at several intermediate stages, but the barrier must remain held until the
# whole filesystem/database operation is finished.
MAINTENANCE_ADVISORY_LOCK = 7_319_824_611
NON_TERMINAL_IMPORT_STATUSES = (
    "queued",
    "preparing",
    "extracting",
    "checking_images",
    "generating_image",
    "validating",
)


@contextmanager
def _database_maintenance_guard(*, shared: bool) -> Iterator[None]:
    if engine.dialect.name != "postgresql":
        yield
        return
    lock_function = "pg_advisory_lock_shared" if shared else "pg_advisory_lock"
    unlock_function = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
    with engine.connect() as connection:
        connection.execute(
            text(f"SELECT {lock_function}(:lock_id)"),
            {"lock_id": MAINTENANCE_ADVISORY_LOCK},
        )
        # PostgreSQL advisory locks are session-scoped and survive commits. End
        # the otherwise idle transaction so work performed through other
        # sessions/connections is not accidentally joined to it.
        connection.commit()
        try:
            yield
        finally:
            connection.execute(
                text(f"SELECT {unlock_function}(:lock_id)"),
                {"lock_id": MAINTENANCE_ADVISORY_LOCK},
            )
            connection.commit()


def acquire_request_barrier(connection: Connection) -> None:
    """Acquire the shared lock on an already checked-out request connection."""
    if engine.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_lock_shared(:lock_id)"),
            {"lock_id": MAINTENANCE_ADVISORY_LOCK},
        )
        # The Session must start and own its own transaction; otherwise its
        # commit would only join this transaction and the Connection context
        # would roll the request back when it closes.
        connection.commit()


def release_request_barrier(connection: Connection) -> None:
    if engine.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_unlock_shared(:lock_id)"),
            {"lock_id": MAINTENANCE_ADVISORY_LOCK},
        )
        connection.commit()


@contextmanager
def database_maintenance_shared_guard() -> Iterator[None]:
    """Allow normal work concurrently while excluding destructive restore."""
    with _database_maintenance_guard(shared=True):
        yield


@contextmanager
def database_maintenance_exclusive_guard() -> Iterator[None]:
    """Drain all in-flight work and exclude new work for restore/snapshot."""
    with _database_maintenance_guard(shared=False):
        yield


# Backwards-compatible name used by the import pipeline. Its semantics are now
# explicitly shared; restore code must use the exclusive guard.
database_maintenance_guard = database_maintenance_shared_guard
