from __future__ import annotations

from pathlib import Path

from rebar_service.postgres_store import PostgresStore


def test_raw_and_smooth_variants_are_first_class_postgres_rows():
    source = (Path(__file__).resolve().parents[1] / "migrations/versions/0001_postgres_storage.py").read_text(encoding="utf-8")
    assert '"task_variants"' in source
    assert 'sa.Column("variant", sa.String(length=16), primary_key=True)' in source
    assert "ck_task_variants_variant" in source


def test_component_and_frontier_storage_are_variant_scoped():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/postgres_store.py").read_text(encoding="utf-8")
    assert "task_id=:task_id AND variant=:variant AND component_id=:component_id" in source
    assert "task_id=:task_id AND variant=:variant\n                    ORDER BY" in source


def test_solution_variant_validation_keeps_raw_and_smooth_separate():
    assert PostgresStore._variant("raw") == "raw"
    assert PostgresStore._variant("smooth") == "smooth"
