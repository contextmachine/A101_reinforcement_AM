"""Add overlay-aware analysis scoping.

Revision ID: 0002_overlay_analyses
Revises: 0001_postgres_storage
Create Date: 2026-09-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_overlay_analyses"
down_revision = "0001_postgres_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_overlay_events",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("overlay_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("idxs", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("real", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "overlay_id", name="uq_task_overlay_event_id"),
        sa.CheckConstraint("event_type IN ('clean','unclean')", name="ck_task_overlay_event_type"),
    )
    op.create_index("ix_task_overlay_events_task_seq", "task_overlay_events", ["task_id", "seq"])

    op.create_table(
        "task_analyses",
        sa.Column("task_id", sa.String(length=32), primary_key=True),
        sa.Column("variant", sa.String(length=16), primary_key=True),
        sa.Column("overlay_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("preparation_state", sa.String(length=64), nullable=False, server_default="stored"),
        sa.Column("frontier_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("effective_rebar_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("active_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("background_only_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("removed_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("degenerate_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id", "variant"], ["task_variants.task_id", "task_variants.variant"], ondelete="CASCADE"),
    )
    op.execute(
        """
        INSERT INTO task_analyses (
            task_id, variant, overlay_id, preparation_state, frontier_version,
            effective_rebar_config, active_indices, background_only_indices,
            removed_indices, degenerate_indices, created_at, updated_at, prepared_at
        )
        SELECT task_id, variant, 0, preparation_state, frontier_version,
               effective_rebar_config, active_indices, background_only_indices,
               '{}'::integer[], degenerate_indices, created_at, updated_at, prepared_at
        FROM task_variants
        """
    )

    for table in ("task_n_requests", "components", "component_results", "runtime_artifacts", "solutions", "task_events"):
        op.add_column(table, sa.Column("overlay_id", sa.BigInteger(), nullable=False, server_default="0"))

    # Repair status labels written by the first PostgreSQL version.  The boolean
    # flags were already correct, but successful optimal rows were serialized as
    # status="feasible".  Keep columns and stored JSON in sync during upgrade.
    op.execute(
        """
        UPDATE solutions
        SET status = CASE
                WHEN is_feasible AND is_optimal THEN 'optimal'
                WHEN is_feasible THEN 'feasible'
                ELSE 'infeasible'
            END,
            result = jsonb_set(
                result, '{status}',
                to_jsonb((CASE
                    WHEN is_feasible AND is_optimal THEN 'optimal'
                    WHEN is_feasible THEN 'feasible'
                    ELSE 'infeasible'
                END)::text), true
            ),
            updated_at = now()
        """
    )
    op.execute(
        """
        UPDATE component_results
        SET status = CASE
                WHEN is_feasible AND is_optimal THEN 'optimal'
                WHEN is_feasible THEN 'feasible'
                ELSE 'infeasible'
            END,
            result = jsonb_set(
                result, '{status}',
                to_jsonb((CASE
                    WHEN is_feasible AND is_optimal THEN 'optimal'
                    WHEN is_feasible THEN 'feasible'
                    ELSE 'infeasible'
                END)::text), true
            ),
            updated_at = now()
        """
    )

    # The old component-results FK depends on the old components primary key.
    op.drop_constraint("component_results_task_id_variant_component_id_fkey", "component_results", type_="foreignkey")

    op.drop_constraint("task_n_requests_pkey", "task_n_requests", type_="primary")
    op.create_primary_key("task_n_requests_pkey", "task_n_requests", ["task_id", "variant", "overlay_id", "n"])
    op.create_foreign_key(
        "fk_task_n_requests_analysis", "task_n_requests", "task_analyses",
        ["task_id", "variant", "overlay_id"], ["task_id", "variant", "overlay_id"], ondelete="CASCADE",
    )
    op.drop_index("ix_task_n_requests_task_variant_position", table_name="task_n_requests")
    op.create_index("ix_task_n_requests_analysis_position", "task_n_requests", ["task_id", "variant", "overlay_id", "position"])

    op.drop_constraint("components_pkey", "components", type_="primary")
    op.create_primary_key("components_pkey", "components", ["task_id", "variant", "overlay_id", "component_id"])
    op.create_foreign_key(
        "fk_components_analysis", "components", "task_analyses",
        ["task_id", "variant", "overlay_id"], ["task_id", "variant", "overlay_id"], ondelete="CASCADE",
    )
    op.drop_index("ix_components_task_variant", table_name="components")
    op.create_index("ix_components_analysis", "components", ["task_id", "variant", "overlay_id"])

    op.drop_constraint("component_results_pkey", "component_results", type_="primary")
    op.create_primary_key(
        "component_results_pkey", "component_results", ["task_id", "variant", "overlay_id", "component_id", "n"]
    )
    op.create_foreign_key(
        "fk_component_results_component", "component_results", "components",
        ["task_id", "variant", "overlay_id", "component_id"],
        ["task_id", "variant", "overlay_id", "component_id"], ondelete="CASCADE",
    )
    op.drop_index("ix_component_results_task_variant_component", table_name="component_results")
    op.create_index(
        "ix_component_results_analysis_component", "component_results", ["task_id", "variant", "overlay_id", "component_id"]
    )

    op.drop_constraint("uq_runtime_artifact_key", "runtime_artifacts", type_="unique")
    op.create_unique_constraint(
        "uq_runtime_artifact_key", "runtime_artifacts", ["task_id", "variant", "overlay_id", "artifact_key"]
    )
    op.create_foreign_key(
        "fk_runtime_artifacts_analysis", "runtime_artifacts", "task_analyses",
        ["task_id", "variant", "overlay_id"], ["task_id", "variant", "overlay_id"], ondelete="CASCADE",
    )
    op.drop_index("ix_runtime_artifacts_task_variant", table_name="runtime_artifacts")
    op.create_index("ix_runtime_artifacts_analysis", "runtime_artifacts", ["task_id", "variant", "overlay_id"])

    op.create_foreign_key(
        "fk_solutions_analysis", "solutions", "task_analyses",
        ["task_id", "variant", "overlay_id"], ["task_id", "variant", "overlay_id"], ondelete="CASCADE",
    )
    op.drop_index("ix_solutions_task_variant", table_name="solutions")
    op.drop_index("ix_solutions_task_variant_source", table_name="solutions")
    op.drop_index("ix_solutions_best", table_name="solutions")
    op.create_index("ix_solutions_analysis", "solutions", ["task_id", "variant", "overlay_id"])
    op.create_index("ix_solutions_analysis_source", "solutions", ["task_id", "variant", "overlay_id", "source"])
    op.create_index(
        "ix_solutions_best", "solutions",
        ["task_id", "variant", "overlay_id", "total_n", "is_feasible", "actual_mass_kg", "proxy_mass"],
    )

    op.create_index("ix_task_events_task_overlay_id", "task_events", ["task_id", "overlay_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_task_events_task_overlay_id", table_name="task_events")

    op.drop_constraint("fk_solutions_analysis", "solutions", type_="foreignkey")
    op.drop_index("ix_solutions_best", table_name="solutions")
    op.drop_index("ix_solutions_analysis_source", table_name="solutions")
    op.drop_index("ix_solutions_analysis", table_name="solutions")
    op.create_index("ix_solutions_task_variant", "solutions", ["task_id", "variant"])
    op.create_index("ix_solutions_task_variant_source", "solutions", ["task_id", "variant", "source"])
    op.create_index("ix_solutions_best", "solutions", ["task_id", "variant", "total_n", "is_feasible", "actual_mass_kg", "proxy_mass"])

    op.drop_constraint("fk_runtime_artifacts_analysis", "runtime_artifacts", type_="foreignkey")
    op.drop_index("ix_runtime_artifacts_analysis", table_name="runtime_artifacts")
    op.drop_constraint("uq_runtime_artifact_key", "runtime_artifacts", type_="unique")
    op.create_unique_constraint("uq_runtime_artifact_key", "runtime_artifacts", ["task_id", "variant", "artifact_key"])
    op.create_index("ix_runtime_artifacts_task_variant", "runtime_artifacts", ["task_id", "variant"])

    op.drop_constraint("fk_component_results_component", "component_results", type_="foreignkey")
    op.drop_index("ix_component_results_analysis_component", table_name="component_results")
    op.drop_constraint("component_results_pkey", "component_results", type_="primary")

    op.drop_constraint("fk_components_analysis", "components", type_="foreignkey")
    op.drop_index("ix_components_analysis", table_name="components")
    op.drop_constraint("components_pkey", "components", type_="primary")
    op.create_primary_key("components_pkey", "components", ["task_id", "variant", "component_id"])

    op.create_primary_key("component_results_pkey", "component_results", ["task_id", "variant", "component_id", "n"])
    op.create_foreign_key(
        "component_results_task_id_variant_component_id_fkey", "component_results", "components",
        ["task_id", "variant", "component_id"], ["task_id", "variant", "component_id"], ondelete="CASCADE",
    )
    op.create_index("ix_component_results_task_variant_component", "component_results", ["task_id", "variant", "component_id"])
    op.create_index("ix_components_task_variant", "components", ["task_id", "variant"])

    op.drop_constraint("fk_task_n_requests_analysis", "task_n_requests", type_="foreignkey")
    op.drop_index("ix_task_n_requests_analysis_position", table_name="task_n_requests")
    op.drop_constraint("task_n_requests_pkey", "task_n_requests", type_="primary")
    op.create_primary_key("task_n_requests_pkey", "task_n_requests", ["task_id", "variant", "n"])
    op.create_index("ix_task_n_requests_task_variant_position", "task_n_requests", ["task_id", "variant", "position"])

    for table in ("task_events", "solutions", "runtime_artifacts", "component_results", "components", "task_n_requests"):
        op.drop_column(table, "overlay_id")

    op.drop_table("task_analyses")
    op.drop_index("ix_task_overlay_events_task_seq", table_name="task_overlay_events")
    op.drop_table("task_overlay_events")
