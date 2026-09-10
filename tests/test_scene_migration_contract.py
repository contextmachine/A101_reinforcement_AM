from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/versions/0003_scenes_immutable_tasks.py"


def test_scene_migration_creates_only_additive_scene_storage_and_nullable_task_context():
    text = MIGRATION.read_text(encoding="utf-8")

    for table in (
        "scenes",
        "scene_sources",
        "scene_variants",
        "scene_components",
        "scene_overlay_events",
    ):
        assert f'op.create_table(\n        "{table}"' in text

    for column in (
        "scene_id",
        "analysis_variant",
        "analysis_overlay_id",
        "component_selection",
    ):
        assert f'op.add_column("tasks", sa.Column("{column}"' in text

    assert 'op.create_foreign_key(' in text and '"fk_tasks_scene"' in text
    assert 'op.create_index("ix_tasks_scene_id"' in text
    assert 'nullable=False' not in "\n".join(
        line for line in text.splitlines() if 'op.add_column("tasks"' in line
    )


def test_scene_migration_does_not_rewrite_or_copy_legacy_rows():
    text = MIGRATION.read_text(encoding="utf-8")

    forbidden = (
        "INSERT INTO scenes",
        "INSERT INTO scene_sources",
        "INSERT INTO scene_variants",
        "INSERT INTO scene_components",
        "INSERT INTO scene_overlay_events",
        "INSERT INTO task_analyses",
        "INSERT INTO task_n_requests",
        "INSERT INTO components",
        "INSERT INTO component_results",
        "INSERT INTO runtime_artifacts",
        "INSERT INTO solutions",
        "UPDATE tasks",
        "UPDATE scenes",
        "DELETE FROM",
        "legacy_canonical_context",
        "legacy-raw0",
        "op.alter_column",
    )
    for marker in forbidden:
        assert marker not in text

    # DDL only: the migration should not execute arbitrary data-manipulation SQL.
    assert "op.execute(" not in text
