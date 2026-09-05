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
