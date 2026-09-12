"""Add durable whole-field API v2 pipeline state.

Revision ID: 0005_v2_pipeline
Revises: 0004_scene_audit_repairs
Create Date: 2026-09-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_v2_pipeline"
down_revision = "0004_scene_audit_repairs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "v2_tasks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("scene_id", sa.String(length=32), sa.ForeignKey("scenes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("overlay_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("smooth", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("preparation_state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("max_useful_n", sa.Integer(), nullable=True),
        sa.Column("preparation_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("preparation_state IN ('pending','preparing','success','error','cancelled')", name="ck_v2_tasks_preparation_state"),
    )
    op.create_index("ix_v2_tasks_scene", "v2_tasks", ["scene_id", "created_at"])

    op.create_table(
        "v2_task_n",
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("v2_tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("n", sa.Integer(), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("fun", sa.Float(), nullable=True),
        sa.Column("mass_kg", sa.Float(), nullable=True),
        sa.Column("mass_bg_kg", sa.Float(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("n > 0", name="ck_v2_task_n_positive"),
        sa.CheckConstraint("attempt > 0", name="ck_v2_task_n_attempt_positive"),
        sa.CheckConstraint("state IN ('pending','preparing','solving','fitting','baring','error','success','cancelled')", name="ck_v2_task_n_state"),
        sa.CheckConstraint("status IS NULL OR status IN ('optimal','feasible','infeasible')", name="ck_v2_task_n_status"),
    )
    op.create_index("ix_v2_task_n_task_position", "v2_task_n", ["task_id", "position"])

    op.create_table(
        "v2_runtime_artifacts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("v2_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact_key", sa.Text(), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("n", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("codec", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "artifact_key", name="uq_v2_runtime_artifact_key"),
    )
    op.create_index("ix_v2_runtime_artifacts_task", "v2_runtime_artifacts", ["task_id"])

    op.create_table(
        "v2_bars_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("scene_id", sa.String(length=32), sa.ForeignKey("scenes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("overlay_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("smooth", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("zones", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('pending','baring','success','error')", name="ck_v2_bars_requests_state"),
    )
    op.create_index("ix_v2_bars_requests_scene", "v2_bars_requests", ["scene_id", "created_at"])

    op.create_table(
        "v2_verification_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("scene_id", sa.String(length=32), sa.ForeignKey("scenes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("overlay_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("smooth", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("zones", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('pending','validation','success','error')", name="ck_v2_verification_requests_state"),
    )
    op.create_index("ix_v2_verification_requests_scene", "v2_verification_requests", ["scene_id", "created_at"])


def downgrade() -> None:
    for table in ("v2_verification_requests", "v2_bars_requests"):
        op.drop_index(f"ix_{table}_scene", table_name=table)
        op.drop_table(table)
    op.drop_index("ix_v2_runtime_artifacts_task", table_name="v2_runtime_artifacts")
    op.drop_table("v2_runtime_artifacts")
    op.drop_index("ix_v2_task_n_task_position", table_name="v2_task_n")
    op.drop_table("v2_task_n")
    op.drop_index("ix_v2_tasks_scene", table_name="v2_tasks")
    op.drop_table("v2_tasks")
