"""Additive storage for the /v2 whole-field pipeline.

Revision ID: 0005_v2_tasks
Revises: 0004_scene_audit_repairs
Create Date: 2026-09-12

Additive-only, like 0003: it creates the ``v2_*`` tables used by the new
minimalist ``/v2`` API and adds one nullable column to
``scene_overlay_events``. No existing row is rewritten and no v1 table is
altered, so the 0002/0003-era service keeps working unchanged.

Table names are unqualified on purpose: ``migrations/env.py`` sets
``search_path`` to the configured application schema before running the
revision, exactly like 0003.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_v2_tasks"
down_revision = "0004_scene_audit_repairs"
branch_labels = None
depends_on = None


def _task_columns() -> list[sa.Column]:
    """Columns shared by the two isolated-worker task tables."""

    return [
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "scene_id",
            sa.String(length=32),
            sa.ForeignKey("scenes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("overlay_id", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("smooth", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "zones",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table(
        "v2_tasks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "scene_id",
            sa.String(length=32),
            sa.ForeignKey("scenes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("overlay_id", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("smooth", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="created"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("max_useful_n", sa.Integer(), nullable=True),
        sa.Column("min_useful_n", sa.Integer(), nullable=True),
        sa.Column("prepare_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cancelled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_v2_tasks_scene_id", "v2_tasks", ["scene_id"])

    op.create_table(
        "v2_task_ns",
        sa.Column(
            "task_id",
            sa.String(length=32),
            sa.ForeignKey("v2_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("n", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("fun", postgresql.DOUBLE_PRECISION(), nullable=True),
        sa.Column("mass_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("n > 0", name="ck_v2_task_ns_positive"),
    )
    op.create_index("ix_v2_task_ns_task_state", "v2_task_ns", ["task_id", "state"])

    op.create_table(
        "v2_artifacts",
        sa.Column(
            "task_id",
            sa.String(length=32),
            sa.ForeignKey("v2_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("codec", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table("v2_bar_tasks", *_task_columns())
    op.create_index("ix_v2_bar_tasks_state", "v2_bar_tasks", ["state"])

    op.create_table("v2_verification_tasks", *_task_columns())
    op.create_index("ix_v2_verification_tasks_state", "v2_verification_tasks", ["state"])

    # Nullable by design: v1 rows keep NULL, the v2 API stores the client clock here.
    op.add_column(
        "scene_overlay_events",
        sa.Column("client_time", postgresql.DOUBLE_PRECISION(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scene_overlay_events", "client_time")

    op.drop_index("ix_v2_verification_tasks_state", table_name="v2_verification_tasks")
    op.drop_table("v2_verification_tasks")
    op.drop_index("ix_v2_bar_tasks_state", table_name="v2_bar_tasks")
    op.drop_table("v2_bar_tasks")
    op.drop_table("v2_artifacts")
    op.drop_index("ix_v2_task_ns_task_state", table_name="v2_task_ns")
    op.drop_table("v2_task_ns")
    op.drop_index("ix_v2_tasks_scene_id", table_name="v2_tasks")
    op.drop_table("v2_tasks")
