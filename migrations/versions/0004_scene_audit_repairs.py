"""Reserved no-op revision for the non-destructive scene-schema rollout.

Revision ID: 0004_scene_audit_repairs
Revises: 0003_scenes_immutable_tasks

The original draft of this revision rewrote historical task data. Production is
still on 0002, and the accepted rollout contract is additive-only, so this
revision intentionally performs no data migration. Keeping the revision id
preserves the repository's migration chain while making ``upgrade head`` safe
for the 0002 production schema.
"""

revision = "0004_scene_audit_repairs"
down_revision = "0003_scenes_immutable_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
