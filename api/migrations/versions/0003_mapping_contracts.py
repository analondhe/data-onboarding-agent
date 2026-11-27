"""mapping_contracts and runs.drift, for recurring loads

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-26 21:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mapping_contracts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_name", sa.String(length=200), nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("target_schema", sa.String(length=100), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("baseline_profile", sa.JSON(), nullable=False),
        sa.Column("mappings", sa.JSON(), nullable=False),
        sa.Column("created_from_run_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_from_run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_name", "source_name", "version"),
    )
    op.add_column("runs", sa.Column("drift", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "drift")
    op.drop_table("mapping_contracts")
