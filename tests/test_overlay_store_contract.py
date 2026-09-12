from __future__ import annotations

import inspect
from contextlib import contextmanager
from pathlib import Path

import pytest

from rebar_service.config import Settings
from rebar_service.overlays import resolve_overlay
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
    assert "overlay_id" in inspect.signature(PostgresStore.mark_analysis_failed).parameters


# ---------- append semantics: server ids, idx filtering, client time ----------
class _FakeOverlayTable:
    """Minimal stand-in for scene_overlay_events (unique (scene_id, overlay_id))."""

    def __init__(self):
        self.rows: list[dict] = []

    def insert(self, params):
        key = (params["scene_id"], params["overlay_id"])
        if any((row["scene_id"], row["overlay_id"]) == key for row in self.rows):
            raise RuntimeError('duplicate key value violates unique constraint "uq_scene_overlay_event_id"')
        self.rows.append(
            {
                "seq": len(self.rows) + 1,
                "scene_id": params["scene_id"],
                "overlay_id": params["overlay_id"],
                "event_type": params["event_type"],
                "idxs": list(params["idxs"]),
                "real": params["real"],
                "client_time": params["client_time"],
                "created_at": 100.0 + len(self.rows),
            }
        )

    def scene_rows(self, scene_id):
        return [row for row in self.rows if row["scene_id"] == scene_id]


class _OverlayResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._scalar


class _OverlayConnection:
    def __init__(self, table):
        self.table = table

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = dict(params or {})
        if sql.startswith("INSERT INTO scene_overlay_events"):
            self.table.insert(params)
            return _OverlayResult()
        if "COALESCE(MAX(overlay_id), 0) + 1" in sql:
            rows = self.table.scene_rows(params["scene_id"])
            return _OverlayResult(scalar=max((row["overlay_id"] for row in rows), default=0) + 1)
        if "FROM scene_overlay_events" in sql:
            return _OverlayResult(rows=self.table.scene_rows(params["scene_id"]))
        return _OverlayResult()


class _OverlayDatabase:
    def __init__(self, table):
        self.table = table

    @contextmanager
    def begin(self):
        yield _OverlayConnection(self.table)

    @contextmanager
    def connect(self):
        yield _OverlayConnection(self.table)


class _SceneOverlayStore(PostgresStore):
    polygon_count = 4

    def get_scene(self, scene_id):
        return {"id": scene_id, "state": "ready"}

    def load_scene_variant_polygons(self, scene_id, *, variant="raw"):
        return [{"points": [[0, 0], [1, 0], [1, 1]], "load": 1.0} for _ in range(self.polygon_count)]


def _overlay_store():
    table = _FakeOverlayTable()
    return _SceneOverlayStore(Settings(), database=_OverlayDatabase(table)), table


def test_append_allocates_server_side_ids_in_append_order():
    store, _ = _overlay_store()

    rows = store.append_scene_overlay_events(
        "scene", [{"type": "clean", "idxs": [0], "real": False}, {"type": "clean", "idxs": [1], "real": True}]
    )

    assert [row["id"] for row in rows] == [1, 2]
    assert [row["seq"] for row in rows] == [1, 2]

    rows = store.append_scene_overlay_events("scene", [{"type": "clean", "idxs": [2], "real": False}])
    assert [row["id"] for row in rows] == [1, 2, 3]


def test_client_supplied_ids_keep_working_and_shift_the_next_allocation():
    store, _ = _overlay_store()

    store.append_scene_overlay_events("scene", [{"id": 999, "type": "clean", "idxs": [0], "real": True}])
    rows = store.append_scene_overlay_events("scene", [{"type": "clean", "idxs": [1], "real": True}])

    assert [row["id"] for row in rows] == [999, 1000]


def test_append_rejects_duplicate_and_reused_client_ids():
    store, _ = _overlay_store()

    with pytest.raises(ValueError):
        store.append_scene_overlay_events(
            "scene",
            [{"id": 5, "type": "clean", "idxs": [0], "real": True},
             {"id": 5, "type": "clean", "idxs": [1], "real": True}],
        )

    store.append_scene_overlay_events("scene", [{"id": 5, "type": "clean", "idxs": [0], "real": True}])
    with pytest.raises(ValueError, match="already exists"):
        store.append_scene_overlay_events("scene", [{"id": 5, "type": "clean", "idxs": [1], "real": True}])


def test_append_rejects_out_of_range_indices_and_unknown_types():
    store, _ = _overlay_store()

    with pytest.raises(ValueError):
        store.append_scene_overlay_events("scene", [{"type": "clean", "idxs": [4], "real": True}])
    with pytest.raises(ValueError):
        store.append_scene_overlay_events("scene", [{"type": "wipe", "idxs": [0], "real": True}])


def test_repeated_masks_are_filtered_but_the_event_is_still_stored():
    store, _ = _overlay_store()

    store.append_scene_overlay_events("scene", [{"type": "clean", "idxs": [0, 1], "real": False}])
    rows = store.append_scene_overlay_events("scene", [{"type": "clean", "idxs": [0, 2], "real": False}])

    assert rows[1]["idxs"] == [2]

    rows = store.append_scene_overlay_events("scene", [{"type": "clean", "idxs": [0], "real": True}])
    assert rows[-1]["idxs"] == []
    assert rows[-1]["id"] == 3


def test_unclean_only_keeps_indices_that_are_actually_masked():
    store, _ = _overlay_store()

    store.append_scene_overlay_events("scene", [{"type": "clean", "idxs": [0], "real": True}])
    rows = store.append_scene_overlay_events("scene", [{"type": "unclean", "idxs": [0, 1], "real": False}])

    assert rows[-1]["idxs"] == [0]

    rows = store.append_scene_overlay_events("scene", [{"type": "unclean", "idxs": [0], "real": False}])
    assert rows[-1]["idxs"] == []


def test_filtering_follows_the_state_built_inside_one_request():
    store, _ = _overlay_store()

    rows = store.append_scene_overlay_events(
        "scene",
        [
            {"type": "clean", "idxs": [0, 1], "real": False},
            {"type": "clean", "idxs": [1, 2], "real": True},
            {"type": "unclean", "idxs": [0, 3], "real": False},
        ],
    )

    assert [row["idxs"] for row in rows] == [[0, 1], [2], [0]]


def test_client_time_is_persisted_and_echoed_as_time():
    store, _ = _overlay_store()

    rows = store.append_scene_overlay_events(
        "scene",
        [{"type": "clean", "idxs": [0], "real": True, "time": 12345654},
         {"type": "clean", "idxs": [1], "real": True}],
    )

    assert rows[0]["time"] == 12345654.0
    assert rows[1]["time"] is None
    assert store.scene_overlay_events("scene")[0]["time"] == 12345654.0


def test_stored_events_resolve_to_the_same_state_as_the_unfiltered_log():
    store, _ = _overlay_store()
    raw = [
        {"type": "clean", "idxs": [0, 1], "real": False},
        {"type": "clean", "idxs": [1], "real": True},
        {"type": "unclean", "idxs": [1], "real": False},
    ]
    stored = store.append_scene_overlay_events("scene", [dict(event) for event in raw])

    polygons = [{"load": 1.0} for _ in range(4)]
    unfiltered = [{**event, "seq": index + 1, "id": index + 1} for index, event in enumerate(raw)]
    expected = [row["overlay_state"] for row in resolve_overlay(polygons, unfiltered, 3)]
    actual = [row["overlay_state"] for row in resolve_overlay(polygons, stored, stored[-1]["id"])]
    assert actual == expected
