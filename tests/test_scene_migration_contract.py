from pathlib import Path


def test_scene_migration_creates_scene_owned_source_and_overlay_tables():
    root = Path(__file__).resolve().parents[1]
    path = root / "migrations/versions/0003_scenes_immutable_tasks.py"
    text = path.read_text(encoding="utf-8")
    for table in (
        "scenes",
        "scene_sources",
        "scene_variants",
        "scene_components",
        "scene_overlay_events",
    ):
        assert f'op.create_table(\n        "{table}"' in text
    assert 'op.add_column("tasks", sa.Column("scene_id"' in text
    assert 'op.add_column("tasks", sa.Column("analysis_variant"' in text
    assert 'op.add_column("tasks", sa.Column("analysis_overlay_id"' in text
    assert 'op.add_column("tasks", sa.Column("component_selection"' in text


def test_scene_migration_backfills_legacy_tasks_and_canonical_raw_zero():
    root = Path(__file__).resolve().parents[1]
    text = (root / "migrations/versions/0003_scenes_immutable_tasks.py").read_text(encoding="utf-8")
    # One deterministic scene per legacy task keeps old task ids valid.
    assert "INSERT INTO scenes" in text
    assert "FROM tasks" in text
    assert "INSERT INTO scene_overlay_events" in text
    assert "FROM task_overlay_events" in text
    # Canonical fallback priority is encoded by variant/overlay ordering.
    assert "CASE WHEN a.variant = 'raw' AND a.overlay_id = 0 THEN 0" in text
    assert "WHEN a.variant = 'smooth' AND a.overlay_id = 0 THEN 1" in text
    assert "initial_variant" in text
    assert "ROW_NUMBER() OVER" in text


def test_legacy_canonical_context_uses_overlay_append_order_and_copies_derived_rows():
    root = Path(__file__).resolve().parents[1]
    text = (root / "migrations/versions/0003_scenes_immutable_tasks.py").read_text(encoding="utf-8")
    # Overlay ids are opaque; fallback #3 must rank latest initial-variant overlay by append seq.
    assert "LEFT JOIN task_overlay_events" in text
    assert "COALESCE(oe.seq, 0)" in text
    assert "ELSE 0 END DESC" in text
    # A synthetic raw/0 analysis is useful only if its old results/artifacts are also readable there.
    for table in (
        "task_n_requests",
        "components",
        "component_results",
        "runtime_artifacts",
        "solutions",
    ):
        assert f"INSERT INTO {table}" in text
    assert "legacy-raw0" in text
    assert "initial_variant = 'raw'" in text


def test_legacy_migration_ensures_scene_raw_variant_after_canonical_fallback():
    root = Path(__file__).resolve().parents[1]
    text = (root / "migrations/versions/0003_scenes_immutable_tasks.py").read_text(encoding="utf-8")
    # If a legacy task had no raw task_variant and smooth/other context was chosen
    # as the canonical compatibility source, the reusable scene must still expose
    # a raw variant because migrated frontend reads default to smooth=false.
    assert "INSERT INTO scene_variants (scene_id, variant, polygons, smoothing_metadata" in text
    assert "SELECT v.task_id, 'raw', v.polygons" in text
    assert "FROM task_variants v" in text
    assert "WHERE v.variant = 'raw'" in text
