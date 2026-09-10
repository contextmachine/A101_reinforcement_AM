"""Repair legacy canonical placeholders and mark scene component backfill.

Kept separate from 0003 so installations that already applied the previous
package receive the same corrections. Original historical rows are retained.
"""
from alembic import op

revision = "0004_scene_audit_repairs"
down_revision = "0003_scenes_immutable_tasks"
branch_labels = None
depends_on = None

# SQL ranking has a transport-independent test with a real SQLite window query.
RANK_ORDER = """
    populated DESC,
    CASE WHEN variant='raw' AND overlay_id=0 THEN 0
         WHEN variant='smooth' AND overlay_id=0 THEN 1
         WHEN variant=original_initial_variant THEN 2 ELSE 3 END,
    CASE WHEN variant=original_initial_variant THEN overlay_seq ELSE 0 END DESC,
    updated_at DESC, variant, overlay_id
"""


def _copy_context(table, columns, conflict, json_column=None):
    selected=[]
    for col in columns:
        if col=='variant': selected.append("'raw'")
        elif col=='overlay_id': selected.append('0')
        elif col==json_column:
            selected.append("x."+col+" || jsonb_build_object('variant','raw','smooth',false,'overlay_id',0,"
                            "'legacy_source_context',jsonb_build_object('variant',c.variant,'overlay_id',c.overlay_id))")
        else: selected.append('x.'+col)
    updates=', '.join(f'{col}=EXCLUDED.{col}' for col in columns if col not in conflict)
    op.execute(f"""
        INSERT INTO {table} ({', '.join(columns)})
        SELECT {', '.join(selected)} FROM scene_audit_context c
        JOIN {table} x ON x.task_id=c.task_id AND x.variant=c.variant AND x.overlay_id=c.overlay_id
        WHERE NOT (c.variant='raw' AND c.overlay_id=0)
        ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {updates}
    """)


def upgrade():
    # 0003 changed initial_variant. Recover the original value from task_created
    # event when present, rather than making opaque overlay ids chronological.
    op.execute("""
        UPDATE scenes s SET metadata=s.metadata || jsonb_build_object(
            'legacy_original_initial_variant', COALESCE(
                s.metadata->>'legacy_original_initial_variant',
                (SELECT e.payload->>'variant' FROM task_events e
                 WHERE e.task_id=s.id AND e.event_type='task_created'
                   AND e.payload->>'variant' IN ('raw','smooth')
                 ORDER BY e.id LIMIT 1),
                'raw'))
        WHERE s.metadata ? 'legacy_task_id'
    """)
    # An installation may have run the earlier 0003 without provenance metadata.
    # Its compatibility clones have deterministic ids, so recover their source.
    op.execute("""
        UPDATE scenes s SET metadata=s.metadata || jsonb_build_object(
            'legacy_0003_selected_context', (
                SELECT jsonb_build_object('variant',o.variant,'overlay_id',o.overlay_id)
                FROM solutions o JOIN solutions c
                  ON c.solution_id=md5(o.task_id || ':' || o.solution_id || ':legacy-raw0')
                 AND c.task_id=o.task_id AND c.variant='raw' AND c.overlay_id=0
                WHERE o.task_id=s.id AND NOT (o.variant='raw' AND o.overlay_id=0)
                ORDER BY o.created_at, o.solution_id LIMIT 1))
        WHERE s.metadata ? 'legacy_task_id'
          AND NOT (s.metadata ? 'legacy_0003_selected_context')
    """)
    op.execute(f"""
        CREATE TEMP TABLE scene_audit_context ON COMMIT DROP AS
        WITH candidates AS (
            SELECT a.task_id, a.variant, a.overlay_id, a.updated_at,
                   COALESCE(s.metadata->>'legacy_original_initial_variant','raw') AS original_initial_variant,
                   COALESCE(oe.seq,0) AS overlay_seq,
                   (NOT (a.variant='raw' AND a.overlay_id=0
                         AND COALESCE(s.metadata->'legacy_0003_selected_context'->>'variant','raw') || ':' ||
                             COALESCE(s.metadata->'legacy_0003_selected_context'->>'overlay_id','0') <> 'raw:0')
                    AND (a.preparation_state IN ('prepared','infeasible') OR a.prepared_at IS NOT NULL
                    OR a.frontier_version > 0 OR a.effective_rebar_config IS NOT NULL
                    OR EXISTS (SELECT 1 FROM solutions x WHERE x.task_id=a.task_id AND x.variant=a.variant AND x.overlay_id=a.overlay_id)
                    OR EXISTS (SELECT 1 FROM component_results x WHERE x.task_id=a.task_id AND x.variant=a.variant AND x.overlay_id=a.overlay_id))) AS populated
            FROM task_analyses a JOIN tasks t ON t.id=a.task_id
            JOIN scenes s ON s.id=t.scene_id
            LEFT JOIN scene_overlay_events oe ON oe.scene_id=s.id AND oe.overlay_id=a.overlay_id
            WHERE s.metadata ? 'legacy_task_id' AND t.id=t.scene_id
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY {RANK_ORDER}) AS rn
            FROM candidates
        ) SELECT task_id, variant, overlay_id FROM ranked WHERE rn=1
    """)
    op.execute("""
        UPDATE scenes s SET metadata=s.metadata || jsonb_build_object(
            'legacy_canonical_source',jsonb_build_object('variant',c.variant,'overlay_id',c.overlay_id))
        FROM scene_audit_context c WHERE c.task_id=s.id
    """)
    _copy_context('task_analyses',
        ['task_id','variant','overlay_id','preparation_state','frontier_version','effective_rebar_config',
         'active_indices','background_only_indices','removed_indices','degenerate_indices',
         'created_at','updated_at','prepared_at'], ['task_id','variant','overlay_id'])
    _copy_context('task_n_requests',
        ['task_id','variant','overlay_id','n','position','status','detail','requested_at','updated_at','cancelled_at'],
        ['task_id','variant','overlay_id','n'])
    _copy_context('components',
        ['task_id','variant','overlay_id','component_id','state','polygon_indices','classes','loads','bounds',
         'demand_bounds','max_useful_n','n_bounds','planned_ns','force_single_box','created_at','updated_at'],
        ['task_id','variant','overlay_id','component_id'])
    _copy_context('component_results',
        ['task_id','variant','overlay_id','component_id','n','status','is_feasible','is_optimal','proxy_mass','result','created_at','updated_at'],
        ['task_id','variant','overlay_id','component_id','n'], json_column='result')
    _copy_context('runtime_artifacts',
        ['task_id','variant','overlay_id','artifact_key','artifact_type','component_id','n','codec','payload','sha256','created_at','updated_at'],
        ['task_id','variant','overlay_id','artifact_key'])
    op.execute("""
        INSERT INTO solutions (solution_id,task_id,variant,overlay_id,source,total_n,component_ns,
            proxy_mass,actual_mass_kg,is_feasible,is_optimal,status,result,validation,created_at,updated_at)
        SELECT md5(x.task_id || ':' || x.solution_id || ':legacy-raw0'), x.task_id,'raw',0,x.source,
            x.total_n,x.component_ns,x.proxy_mass,x.actual_mass_kg,x.is_feasible,x.is_optimal,x.status,
            x.result || jsonb_build_object('solution_id',md5(x.task_id || ':' || x.solution_id || ':legacy-raw0'),
                'variant','raw','smooth',false,'overlay_id',0,
                'legacy_source_context',jsonb_build_object('variant',c.variant,'overlay_id',c.overlay_id)),
            x.validation,x.created_at,x.updated_at
        FROM scene_audit_context c JOIN solutions x
          ON x.task_id=c.task_id AND x.variant=c.variant AND x.overlay_id=c.overlay_id
        WHERE NOT (c.variant='raw' AND c.overlay_id=0)
        ON CONFLICT (solution_id) DO NOTHING
    """)
    # These legacy scene components were copied from config-dependent demand
    # components by 0003. Rebuild scene-owned physical components in a worker.
    # Do NOT delete/renumber historical task components or results.
    op.execute("""
        DELETE FROM scene_components c USING scenes s
        WHERE c.scene_id=s.id AND s.metadata ? 'legacy_task_id'
    """)
    op.execute("""
        UPDATE scenes SET state='preparing',
            metadata=metadata || jsonb_build_object('needs_component_backfill',true)
        WHERE metadata ? 'legacy_task_id'
    """)


def downgrade():
    # Copies preserve source data and must not be deleted on a downgrade:
    # callers may already have bookmarked their deterministic solution ids.
    pass
