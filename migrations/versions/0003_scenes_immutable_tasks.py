"""Add reusable scenes and immutable task context without rewriting legacy rows.

Revision ID: 0003_scenes_immutable_tasks
Revises: 0002_overlay_analyses
Create Date: 2026-09-10

This migration is intentionally additive-only. Existing rows created by the
0002-era service are left byte-for-byte untouched. New scene-aware application
code may populate the new tables and nullable task context columns for new
requests while the old service can continue using the pre-existing tables.
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
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "scene_sources",
        sa.Column(
            "scene_id",
            sa.String(length=32),
            sa.ForeignKey("scenes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "scene_variants",
        sa.Column(
            "scene_id",
            sa.String(length=32),
            sa.ForeignKey("scenes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("variant", sa.String(length=16), primary_key=True),
        sa.Column(
            "polygons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("smoothing_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("variant IN ('raw','smooth')", name="ck_scene_variants_variant"),
    )

    op.create_table(
        "scene_components",
        sa.Column(
            "scene_id",
            sa.String(length=32),
            sa.ForeignKey("scenes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("component_id", sa.Integer(), primary_key=True),
        sa.Column(
            "polygon_indices",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            server_default=sa.text("'{}'::integer[]"),
        ),
        sa.Column("bounds", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scene_components_scene", "scene_components", ["scene_id", "component_id"])

    op.create_table(
        "scene_overlay_events",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "scene_id",
            sa.String(length=32),
            sa.ForeignKey("scenes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("overlay_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column(
            "idxs",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            server_default=sa.text("'{}'::integer[]"),
        ),
        sa.Column("real", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("scene_id", "overlay_id", name="uq_scene_overlay_event_id"),
        sa.CheckConstraint("event_type IN ('clean','unclean')", name="ck_scene_overlay_event_type"),
    )
    op.create_index("ix_scene_overlay_events_scene_seq", "scene_overlay_events", ["scene_id", "seq"])

    # Nullable by design: legacy 0002 rows keep NULL and are not backfilled.
    op.add_column("tasks", sa.Column("scene_id", sa.String(length=32), nullable=True))
    op.add_column("tasks", sa.Column("analysis_variant", sa.String(length=16), nullable=True))
    op.add_column("tasks", sa.Column("analysis_overlay_id", sa.BigInteger(), nullable=True))
    op.add_column("tasks", sa.Column("component_selection", postgresql.ARRAY(sa.Integer()), nullable=True))

    # NULL legacy values satisfy both constraints. New scene-aware rows get
    # referential integrity without forcing migration of historical tasks.
    op.create_foreign_key(
        "fk_tasks_scene",
        "tasks",
        "scenes",
        ["scene_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_tasks_scene_id", "tasks", ["scene_id"])
    op.create_check_constraint(
        "ck_tasks_analysis_variant",
        "tasks",
        "analysis_variant IS NULL OR analysis_variant IN ('raw','smooth')",
    )


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
