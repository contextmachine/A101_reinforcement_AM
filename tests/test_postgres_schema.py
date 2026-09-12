from pathlib import Path

from rebar_service.config import Settings


def test_database_url_uses_existing_kubernetes_postgres_service():
    settings = Settings(
        postgres_host="a101-postgres",
        postgres_port=5432,
        postgres_db="a101",
        postgres_user="rebar-user",
        postgres_password="p@ss/word",
    )
    url = settings.database_url
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "a101-postgres"
    assert url.port == 5432
    assert url.database == "a101"
    assert url.username == "rebar-user"
    assert url.password == "p@ss/word"


def test_initial_migration_contains_all_durable_tables_and_indexes():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0001_postgres_storage.py"
    text = path.read_text(encoding="utf-8")
    for table in (
        "tasks",
        "task_sources",
        "task_variants",
        "task_n_requests",
        "components",
        "component_results",
        "runtime_artifacts",
        "solutions",
        "task_events",
    ):
        assert f'create_table(\n        "{table}"' in text
    assert "ix_solutions_best" in text
    assert "ix_task_events_task_id_id" in text


def test_task_sources_has_integrity_checksum():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/versions/0001_postgres_storage.py").read_text(encoding="utf-8")
    store = (root / "rebar_service/postgres_store.py").read_text(encoding="utf-8")
    assert 'sa.Column("sha256", sa.String(length=64), nullable=False)' in migration
    assert "INSERT INTO task_sources (task_id, kind, filename, content, sha256, metadata)" in store


def test_variant_scoped_tables_are_relationally_tied_to_task_variants():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/versions/0001_postgres_storage.py").read_text(encoding="utf-8")
    variant_fk = 'sa.ForeignKeyConstraint(["task_id", "variant"], ["task_variants.task_id", "task_variants.variant"], ondelete="CASCADE")'
    assert migration.count(variant_fk) >= 4
    component_fk = 'sa.ForeignKeyConstraint(["task_id", "variant", "component_id"], ["components.task_id", "components.variant", "components.component_id"], ondelete="CASCADE")'
    assert component_fk in migration


def test_tasks_table_has_no_unstructured_extra_metadata_bucket():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/versions/0001_postgres_storage.py").read_text(encoding="utf-8")
    tasks_section = migration.split('"tasks",', 1)[1].split('op.create_table(', 1)[0]
    assert 'sa.Column("extra"' not in tasks_section


def test_overlay_migration_scopes_all_derived_analysis_tables():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/versions/0002_overlay_analyses.py").read_text(encoding="utf-8")
    assert 'op.create_table(\n        "task_overlay_events"' in migration
    assert 'op.create_table(\n        "task_analyses"' in migration
    for table in ("task_n_requests", "components", "component_results", "runtime_artifacts", "solutions", "task_events"):
        assert f'"{table}"' in migration
    assert 'op.add_column(table, sa.Column("overlay_id"' in migration


def test_application_postgres_schema_is_rebar():
    settings = Settings()
    assert settings.postgres_schema == "rebar"


def test_overlay_migration_normalizes_existing_optimal_statuses():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/versions/0002_overlay_analyses.py").read_text(encoding="utf-8")
    assert "UPDATE solutions" in migration
    assert "UPDATE component_results" in migration
    assert "jsonb_set" in migration
    assert "WHEN is_feasible AND is_optimal THEN 'optimal'" in migration


def test_v2_migration_is_additive_and_chains_after_the_scene_revisions():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/versions/0005_v2_tasks.py").read_text(encoding="utf-8")

    assert 'revision = "0005_v2_tasks"' in migration
    assert 'down_revision = "0004_scene_audit_repairs"' in migration

    for table in ("v2_tasks", "v2_task_ns", "v2_artifacts", "v2_bar_tasks", "v2_verification_tasks"):
        assert f'"{table}"' in migration
        assert f'op.drop_table("{table}")' in migration

    # The only change to a pre-existing table is one nullable column.
    assert 'op.add_column(\n        "scene_overlay_events",' in migration
    assert 'sa.Column("client_time", postgresql.DOUBLE_PRECISION(), nullable=True)' in migration
    assert 'op.drop_column("scene_overlay_events", "client_time")' in migration
    forbidden = ("op.alter_column", "op.execute(", "INSERT INTO", "UPDATE ", "DELETE FROM")
    for marker in forbidden + ("op.drop_constraint",):
        assert marker not in migration


def test_v2_tables_are_unqualified_so_the_search_path_selects_the_schema():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/versions/0005_v2_tasks.py").read_text(encoding="utf-8")
    assert "schema=" not in migration
    assert 'sa.ForeignKey("scenes.id", ondelete="RESTRICT")' in migration
    assert 'sa.ForeignKey("v2_tasks.id", ondelete="CASCADE")' in migration
    assert 'sa.CheckConstraint("n > 0", name="ck_v2_task_ns_positive")' in migration


def test_migration_head_is_the_v2_revision():
    root = Path(__file__).resolve().parents[1]
    versions = root / "migrations/versions"
    revisions = {}
    for path in versions.glob("0*.py"):
        text = path.read_text(encoding="utf-8")
        revision = text.split('revision = "', 1)[1].split('"', 1)[0]
        down = text.split("down_revision = ", 1)[1].split("\n", 1)[0].strip().strip('"')
        revisions[revision] = None if down == "None" else down
    heads = set(revisions) - {down for down in revisions.values() if down}
    assert heads == {"0005_v2_tasks"}
