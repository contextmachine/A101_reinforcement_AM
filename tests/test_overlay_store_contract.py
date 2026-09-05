from __future__ import annotations

import inspect
from pathlib import Path

from rebar_service.postgres_store import PostgresStore


def test_store_exposes_overlay_event_and_analysis_methods():
    for name in (
        "append_overlay_events",
        "overlay_events",
        "resolved_source_polygons",
        "ensure_analysis",
        "analysis_state",
        "mark_analysis_preparing",
        "mark_analysis_prepared",
    ):
        assert hasattr(PostgresStore, name), name


def test_derived_storage_methods_accept_overlay_id():
    for name in (
        "requested_ns",
        "add_requested_ns",
        "set_n_status",
        "get_n_statuses",
        "save_field",
        "load_field",
        "save_component",
        "load_component",
        "component_ids",
        "save_problem",
        "load_problem",
        "save_solver_result",
        "load_solver_result",
        "save_frontier_result",
        "frontier_version",
        "load_frontier",
        "all_frontiers",
        "solutions",
        "best_solution",
    ):
        signature = inspect.signature(getattr(PostgresStore, name))
        assert "overlay_id" in signature.parameters, name


def test_store_sql_conflict_keys_include_overlay_dimension():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/postgres_store.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (task_id, variant, overlay_id, n)" in source
    assert "ON CONFLICT (task_id, variant, overlay_id, component_id)" in source
    assert "ON CONFLICT (task_id, variant, overlay_id, component_id, n)" in source
    assert "overlay_id=:overlay_id" in source


def test_store_can_mark_overlay_analysis_failed_for_retry():
    import inspect
    from rebar_service.postgres_store import PostgresStore
    assert "overlay_id" in inspect.signature(PostgresStore.mark_analysis_failed).parameters
