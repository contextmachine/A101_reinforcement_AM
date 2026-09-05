"""Create PostgreSQL durable storage.

Revision ID: 0001_postgres_storage
Revises:
Create Date: 2026-09-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_postgres_storage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("n_mode", sa.String(length=32), nullable=False),
        sa.Column("n_source", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scan_mode", sa.String(length=32), nullable=False),
        sa.Column("whole", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("component_result_top_k", sa.Integer(), nullable=False),
        sa.Column("validate_results", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=False),
        sa.Column("manual_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("initial_variant", sa.String(length=16), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "task_sources",
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    op.create_table(
        "task_variants",
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("variant", sa.String(length=16), primary_key=True),
        sa.Column("polygons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effective_rebar_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("preparation_state", sa.String(length=64), nullable=False, server_default="stored"),
        sa.Column("frontier_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("active_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("background_only_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("degenerate_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("smoothing_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("variant IN ('raw','smooth')", name="ck_task_variants_variant"),
    )

    op.create_table(
        "task_n_requests",
        sa.Column("task_id", sa.String(length=32), primary_key=True),
        sa.Column("variant", sa.String(length=16), primary_key=True),
        sa.Column("n", sa.Integer(), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="requested"),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("n > 0", name="ck_task_n_requests_positive"),
        sa.ForeignKeyConstraint(["task_id", "variant"], ["task_variants.task_id", "task_variants.variant"], ondelete="CASCADE"),
    )
    op.create_index("ix_task_n_requests_task_variant_position", "task_n_requests", ["task_id", "variant", "position"])

    op.create_table(
        "components",
        sa.Column("task_id", sa.String(length=32), primary_key=True),
        sa.Column("variant", sa.String(length=16), primary_key=True),
        sa.Column("component_id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("polygon_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("classes", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("loads", postgresql.ARRAY(sa.Float()), nullable=False, server_default=sa.text("'{}'::double precision[]")),
        sa.Column("bounds", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("demand_bounds", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("max_useful_n", sa.Integer(), nullable=True),
        sa.Column("n_bounds", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("planned_ns", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("force_single_box", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id", "variant"], ["task_variants.task_id", "task_variants.variant"], ondelete="CASCADE"),
    )
    op.create_index("ix_components_task_variant", "components", ["task_id", "variant"])

    op.create_table(
        "component_results",
        sa.Column("task_id", sa.String(length=32), primary_key=True),
        sa.Column("variant", sa.String(length=16), primary_key=True),
        sa.Column("component_id", sa.Integer(), primary_key=True),
        sa.Column("n", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("is_feasible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_optimal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("proxy_mass", sa.Float(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id", "variant", "component_id"], ["components.task_id", "components.variant", "components.component_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_component_results_task_variant_component", "component_results", ["task_id", "variant", "component_id"])

    op.create_table(
        "runtime_artifacts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("variant", sa.String(length=16), nullable=False),
        sa.Column("artifact_key", sa.Text(), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("component_id", sa.Integer(), nullable=True),
        sa.Column("n", sa.Integer(), nullable=True),
        sa.Column("codec", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "variant", "artifact_key", name="uq_runtime_artifact_key"),
        sa.ForeignKeyConstraint(["task_id", "variant"], ["task_variants.task_id", "task_variants.variant"], ondelete="CASCADE"),
    )
    op.create_index("ix_runtime_artifacts_task_variant", "runtime_artifacts", ["task_id", "variant"])

    op.create_table(
        "solutions",
        sa.Column("solution_id", sa.String(length=40), primary_key=True),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("variant", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("total_n", sa.Integer(), nullable=False),
        sa.Column("component_ns", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("proxy_mass", sa.Float(), nullable=True),
        sa.Column("actual_mass_kg", sa.Float(), nullable=True),
        sa.Column("is_feasible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_optimal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id", "variant"], ["task_variants.task_id", "task_variants.variant"], ondelete="CASCADE"),
    )
    op.create_index("ix_solutions_task_variant", "solutions", ["task_id", "variant"])
    op.create_index("ix_solutions_task_variant_source", "solutions", ["task_id", "variant", "source"])
    op.create_index(
        "ix_solutions_best",
        "solutions",
        ["task_id", "variant", "total_n", "is_feasible", "actual_mass_kg", "proxy_mass"],
    )

    op.create_table(
        "task_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_events_task_id_id", "task_events", ["task_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_task_events_task_id_id", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_solutions_best", table_name="solutions")
    op.drop_index("ix_solutions_task_variant_source", table_name="solutions")
    op.drop_index("ix_solutions_task_variant", table_name="solutions")
    op.drop_table("solutions")
    op.drop_index("ix_runtime_artifacts_task_variant", table_name="runtime_artifacts")
    op.drop_table("runtime_artifacts")
    op.drop_index("ix_component_results_task_variant_component", table_name="component_results")
    op.drop_table("component_results")
    op.drop_index("ix_components_task_variant", table_name="components")
    op.drop_table("components")
    op.drop_index("ix_task_n_requests_task_variant_position", table_name="task_n_requests")
    op.drop_table("task_n_requests")
    op.drop_table("task_variants")
    op.drop_table("task_sources")
    op.drop_table("tasks")
