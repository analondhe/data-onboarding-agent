"""runs.trace_experiment_id, for links that open a trace in the MLflow UI

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26 20:10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("trace_experiment_id", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "trace_experiment_id")
