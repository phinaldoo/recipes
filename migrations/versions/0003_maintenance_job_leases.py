"""add renewable leases for backup and restore workers

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backup_restore_jobs", sa.Column("lease_token", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "backup_restore_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_backup_restore_jobs_lease_token",
        "backup_restore_jobs",
        ["lease_token"],
        unique=False,
    )
    op.create_index(
        "ix_backup_restore_jobs_lease_expires_at",
        "backup_restore_jobs",
        ["lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_backup_restore_jobs_lease_expires_at", table_name="backup_restore_jobs")
    op.drop_index("ix_backup_restore_jobs_lease_token", table_name="backup_restore_jobs")
    op.drop_column("backup_restore_jobs", "lease_expires_at")
    op.drop_column("backup_restore_jobs", "lease_token")
