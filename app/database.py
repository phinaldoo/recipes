from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    if engine.dialect.name != "postgresql":
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
        return

    # Keep one physical connection checked out for the complete request. A
    # session-level advisory lock would otherwise leak back into the pool when
    # route code calls commit(), or be released too early on a different
    # transaction. GET requests are included because authentication refreshes
    # session timestamps and can therefore write as well.
    from app.maintenance import acquire_request_barrier, release_request_barrier

    with engine.connect() as connection:
        acquire_request_barrier(connection)
        db = SessionLocal(bind=connection)
        try:
            yield db
        finally:
            db.close()
            release_request_barrier(connection)
