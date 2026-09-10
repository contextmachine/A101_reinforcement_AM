"""Add reusable scenes and immutable task analysis context.

Revision ID: 0003_scenes_immutable_tasks
Revises: 0002_overlay_analyses
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_scenes_immutable_tasks"
down_revision = "0002_overlay_analyses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scenes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("state", sa.String(length=64), nullable=False, server_default="preparing"),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "scene_sources",
        sa.Column("scene_id", sa.String(length=32), sa.ForeignKey("scenes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_table(
        "scene_variants",
        sa.Column("scene_id", sa.String(length=32), sa.ForeignKey("scenes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("variant", sa.String(length=16), primary_key=True),
        sa.Column("polygons", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("smoothing_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("variant IN ('raw','smooth')", name="ck_scene_variants_variant"),
    )
    op.create_table(
        "scene_components",
        sa.Column("scene_id", sa.String(length=32), sa.ForeignKey("scenes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("component_id", sa.Integer(), primary_key=True),
        sa.Column("polygon_indices", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("bounds", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scene_components_scene", "scene_components", ["scene_id", "component_id"])

    op.create_table(
        "scene_overlay_events",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("scene_id", sa.String(length=32), sa.ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("overlay_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("idxs", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("real", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("scene_id", "overlay_id", name="uq_scene_overlay_event_id"),
        sa.CheckConstraint("event_type IN ('clean','unclean')", name="ck_scene_overlay_event_type"),
    )
    op.create_index("ix_scene_overlay_events_scene_seq", "scene_overlay_events", ["scene_id", "seq"])

    op.add_column("tasks", sa.Column("scene_id", sa.String(length=32), nullable=True))
    op.add_column("tasks", sa.Column("analysis_variant", sa.String(length=16), nullable=True))
    op.add_column("tasks", sa.Column("analysis_overlay_id", sa.BigInteger(), nullable=True))
    op.add_column("tasks", sa.Column("component_selection", postgresql.ARRAY(sa.Integer()), nullable=True))

    # Backfill one scene per legacy task. Reusing the task id is deterministic,
    # collision-free inside the new scene namespace, and keeps migration cheap.
    op.execute(
        """
        INSERT INTO scenes (id, state, metadata, created_at, updated_at)
        SELECT id, 'ready', jsonb_build_object('legacy_task_id', id, 'legacy_original_initial_variant', initial_variant), created_at, updated_at
        FROM tasks
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO scene_sources (scene_id, kind, filename, content, sha256, metadata)
        SELECT task_id, kind, filename, content, sha256, metadata
        FROM task_sources
        ON CONFLICT (scene_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO scene_variants (scene_id, variant, polygons, smoothing_metadata, created_at, updated_at)
        SELECT task_id, variant, polygons, smoothing_metadata, created_at, updated_at
        FROM task_variants
        ON CONFLICT (scene_id, variant) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO scene_overlay_events (scene_id, overlay_id, event_type, idxs, real, created_at)
        SELECT task_id, overlay_id, event_type, idxs, real, created_at
        FROM task_overlay_events
        ORDER BY task_id, seq
        ON CONFLICT (scene_id, overlay_id) DO NOTHING
        """
    )

    # Pick one deterministic source context for the legacy raw/0 compatibility
    # snapshot. Overlay ids are opaque, therefore fallback #3 uses append seq,
    # never numeric overlay_id ordering.
    op.execute(
        """
        CREATE TEMP TABLE legacy_canonical_context ON COMMIT DROP AS
        WITH ranked AS (
            SELECT
                a.task_id,
                a.variant,
                a.overlay_id,
                ROW_NUMBER() OVER (
                    PARTITION BY a.task_id
                    ORDER BY
                        CASE WHEN a.variant = 'raw' AND a.overlay_id = 0 THEN 0
                             WHEN a.variant = 'smooth' AND a.overlay_id = 0 THEN 1
                             WHEN a.variant = t.initial_variant THEN 2
                             ELSE 3 END,
                        CASE WHEN a.variant = t.initial_variant THEN COALESCE(oe.seq, 0) ELSE 0 END DESC,
                        a.updated_at DESC
                ) AS rn
            FROM task_analyses a
            JOIN tasks t ON t.id = a.task_id
            LEFT JOIN task_overlay_events oe
              ON oe.task_id = a.task_id AND oe.overlay_id = a.overlay_id
        )
        SELECT task_id, variant, overlay_id
        FROM ranked
        WHERE rn = 1
        """
    )

    # Keep provenance before any canonical copies or initial_variant rewrite.
    # 0004 can distinguish an original raw/0 analysis from this compatibility alias.
    op.execute(
        """
        UPDATE scenes s SET metadata=s.metadata || jsonb_build_object(
            'legacy_0003_selected_context', jsonb_build_object('variant', c.variant, 'overlay_id', c.overlay_id))
        FROM legacy_canonical_context c WHERE c.task_id=s.id
        """
    )

    # Raw task_variants normally exists for every historical task. Keep the
    # migration robust for partially-imported data by cloning the selected
    # variant when raw itself is missing; task_analyses has an FK to it.
    op.execute(
        """
        INSERT INTO task_variants (
            task_id, variant, polygons, effective_rebar_config, preparation_state,
            frontier_version, active_indices, background_only_indices,
            degenerate_indices, smoothing_metadata, created_at, updated_at, prepared_at
        )
        SELECT v.task_id, 'raw', v.polygons, v.effective_rebar_config,
               v.preparation_state, v.frontier_version, v.active_indices,
               v.background_only_indices, v.degenerate_indices, NULL,
               v.created_at, v.updated_at, v.prepared_at
        FROM legacy_canonical_context c
        JOIN task_variants v
          ON v.task_id=c.task_id AND v.variant=c.variant
        ON CONFLICT (task_id, variant) DO NOTHING
        """
    )

    # The scene copy above happened before the compatibility raw task_variant
    # fallback was created. Ensure every migrated scene also has a reusable raw
    # variant, otherwise GET /scenes/{id}/polygons?smooth=false would fail for
    # historical tasks that originally had only smooth/other variants.
    op.execute(
        """
        INSERT INTO scene_variants (scene_id, variant, polygons, smoothing_metadata, created_at, updated_at)
        SELECT v.task_id, 'raw', v.polygons, NULL, v.created_at, v.updated_at
        FROM task_variants v
        WHERE v.variant = 'raw'
        ON CONFLICT (scene_id, variant) DO NOTHING
        """
    )

    # Every migrated task exposes raw/0 as the canonical old-frontend context.
    op.execute(
        """
        UPDATE tasks t
        SET scene_id = t.id,
            initial_variant = 'raw',
            analysis_variant = 'raw',
            analysis_overlay_id = 0,
            component_selection = CASE WHEN t.whole THEN ARRAY[-2]::integer[] ELSE ARRAY[-3]::integer[] END
        FROM legacy_canonical_context c
        WHERE c.task_id = t.id
        """
    )
    # Tasks with no task_analyses are still linked and canonicalized.
    op.execute(
        """
        UPDATE tasks
        SET scene_id = id,
            initial_variant = 'raw',
            analysis_variant = COALESCE(analysis_variant, 'raw'),
            analysis_overlay_id = COALESCE(analysis_overlay_id, 0),
            component_selection = COALESCE(component_selection,
                CASE WHEN whole THEN ARRAY[-2]::integer[] ELSE ARRAY[-3]::integer[] END)
        WHERE scene_id IS NULL
        """
    )

    # Create the canonical analysis row from the chosen historical context.
    # Existing raw/0 is left untouched.
    op.execute(
        """
        INSERT INTO task_analyses (
            task_id, variant, overlay_id, preparation_state, frontier_version,
            effective_rebar_config, active_indices, background_only_indices,
            removed_indices, degenerate_indices, created_at, updated_at, prepared_at
        )
        SELECT a.task_id, 'raw', 0, a.preparation_state, a.frontier_version,
               a.effective_rebar_config, a.active_indices, a.background_only_indices,
               a.removed_indices, a.degenerate_indices, a.created_at, a.updated_at, a.prepared_at
        FROM legacy_canonical_context c
        JOIN task_analyses a
          ON a.task_id=c.task_id AND a.variant=c.variant AND a.overlay_id=c.overlay_id
        ON CONFLICT (task_id, variant, overlay_id) DO NOTHING
        """
    )

    # Copy/relabel all derived rows needed by the historical result API. This
    # preserves the original context and makes the canonical raw/0 context
    # genuinely readable rather than creating an empty alias analysis.
    op.execute(
        """
        INSERT INTO task_n_requests (
            task_id, variant, overlay_id, n, position, status, detail,
            requested_at, updated_at, cancelled_at
        )
        SELECT r.task_id, 'raw', 0, r.n, r.position, r.status, r.detail,
               r.requested_at, r.updated_at, r.cancelled_at
        FROM legacy_canonical_context c
        JOIN task_n_requests r
          ON r.task_id=c.task_id AND r.variant=c.variant AND r.overlay_id=c.overlay_id
        ON CONFLICT (task_id, variant, overlay_id, n) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO components (
            task_id, variant, overlay_id, component_id, state, polygon_indices,
            classes, loads, bounds, demand_bounds, max_useful_n, n_bounds,
            planned_ns, force_single_box, created_at, updated_at
        )
        SELECT x.task_id, 'raw', 0, x.component_id, x.state, x.polygon_indices,
               x.classes, x.loads, x.bounds, x.demand_bounds, x.max_useful_n, x.n_bounds,
               x.planned_ns, x.force_single_box, x.created_at, x.updated_at
        FROM legacy_canonical_context c
        JOIN components x
          ON x.task_id=c.task_id AND x.variant=c.variant AND x.overlay_id=c.overlay_id
        ON CONFLICT (task_id, variant, overlay_id, component_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO component_results (
            task_id, variant, overlay_id, component_id, n, status, is_feasible,
            is_optimal, proxy_mass, result, created_at, updated_at
        )
        SELECT r.task_id, 'raw', 0, r.component_id, r.n, r.status, r.is_feasible,
               r.is_optimal, r.proxy_mass,
               (r.result || jsonb_build_object('variant','raw','smooth',false,'overlay_id',0)),
               r.created_at, r.updated_at
        FROM legacy_canonical_context c
        JOIN component_results r
          ON r.task_id=c.task_id AND r.variant=c.variant AND r.overlay_id=c.overlay_id
        ON CONFLICT (task_id, variant, overlay_id, component_id, n) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO runtime_artifacts (
            task_id, variant, overlay_id, artifact_key, artifact_type,
            component_id, n, codec, payload, sha256, created_at, updated_at
        )
        SELECT r.task_id, 'raw', 0, r.artifact_key, r.artifact_type,
               r.component_id, r.n, r.codec, r.payload, r.sha256, r.created_at, r.updated_at
        FROM legacy_canonical_context c
        JOIN runtime_artifacts r
          ON r.task_id=c.task_id AND r.variant=c.variant AND r.overlay_id=c.overlay_id
        ON CONFLICT (task_id, variant, overlay_id, artifact_key) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO solutions (
            solution_id, task_id, variant, overlay_id, source, total_n, component_ns,
            proxy_mass, actual_mass_kg, is_feasible, is_optimal, status, result,
            validation, created_at, updated_at
        )
        SELECT
            md5(s.task_id || ':' || s.solution_id || ':legacy-raw0'),
            s.task_id, 'raw', 0, s.source, s.total_n, s.component_ns,
            s.proxy_mass, s.actual_mass_kg, s.is_feasible, s.is_optimal, s.status,
            (
                s.result || jsonb_build_object(
                    'solution_id', md5(s.task_id || ':' || s.solution_id || ':legacy-raw0'),
                    'variant', 'raw', 'smooth', false, 'overlay_id', 0
                )
            ),
            s.validation, s.created_at, s.updated_at
        FROM legacy_canonical_context c
        JOIN solutions s
          ON s.task_id=c.task_id AND s.variant=c.variant AND s.overlay_id=c.overlay_id
        WHERE NOT (c.variant='raw' AND c.overlay_id=0)
        ON CONFLICT (solution_id) DO NOTHING
        """
    )

    # Stable component membership for legacy scenes is copied from the
    # canonical compatibility rows. New scenes compute this from source/raw at
    # materialization time and never recalculate it for smooth/overlays.
    op.execute(
        """
        INSERT INTO scene_components (scene_id, component_id, polygon_indices, bounds)
        SELECT c.task_id, c.component_id, c.polygon_indices, c.bounds
        FROM components c
        WHERE c.variant='raw' AND c.overlay_id=0 AND c.component_id >= 0
        ON CONFLICT (scene_id, component_id) DO NOTHING
        """
    )

    op.create_foreign_key("fk_tasks_scene", "tasks", "scenes", ["scene_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_tasks_scene_id", "tasks", ["scene_id"])
    op.alter_column("tasks", "scene_id", nullable=False)
    op.alter_column("tasks", "analysis_variant", nullable=False)
    op.alter_column("tasks", "analysis_overlay_id", nullable=False)
    op.alter_column("tasks", "component_selection", nullable=False)
    op.create_check_constraint("ck_tasks_analysis_variant", "tasks", "analysis_variant IN ('raw','smooth')")


def downgrade() -> None:
    op.drop_constraint("ck_tasks_analysis_variant", "tasks", type_="check")
    op.drop_index("ix_tasks_scene_id", table_name="tasks")
    op.drop_constraint("fk_tasks_scene", "tasks", type_="foreignkey")
    op.drop_column("tasks", "component_selection")
    op.drop_column("tasks", "analysis_overlay_id")
    op.drop_column("tasks", "analysis_variant")
    op.drop_column("tasks", "scene_id")
    op.drop_index("ix_scene_overlay_events_scene_seq", table_name="scene_overlay_events")
    op.drop_table("scene_overlay_events")
    op.drop_index("ix_scene_components_scene", table_name="scene_components")
    op.drop_table("scene_components")
    op.drop_table("scene_variants")
    op.drop_table("scene_sources")
    op.drop_table("scenes")
