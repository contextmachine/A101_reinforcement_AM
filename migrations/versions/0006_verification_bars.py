"""Verification by rods: optional ``bars`` payload on the isolated-worker task tables.

POST /v2/verification may carry the laid-out bars of a solution instead of (or besides) zones;
the worker then verifies those rods as they are, without re-laying out the zones.

Revision ID: 0006_verification_bars
Revises: 0005_v2_tasks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_verification_bars"
down_revision = "0005_v2_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("v2_bar_tasks", "v2_verification_tasks"):
        op.add_column(table, sa.Column("bars", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    for table in ("v2_verification_tasks", "v2_bar_tasks"):
        op.drop_column(table, "bars")
