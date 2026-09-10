from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from rebar_service import api
from rebar_service.models import WsCommand
from rebar_service.postgres_store import PostgresStore


def test_legacy_results_list_keeps_mapping_shape_and_adds_resolved_context(monkeypatch):
    class FakeStore:
        def get_meta(self, task_id):
            return {
                "scene_id": task_id,
                "analysis_variant": "raw",
                "analysis_overlay_id": 0,
                "initial_variant": "smooth",
            }

        def resolve_scene_overlay_id(self, scene_id, selector):
            assert scene_id == "legacy"
            assert selector == -1
            return 777

        def get_result_metas(self, task_id, *, variant, overlay_id):
            assert (task_id, variant, overlay_id) == ("legacy", "smooth", 777)
            return {
                "2": {"n": 2, "kind": "final", "is_feasible": True},
                "5": {"n": 5, "kind": "final", "is_feasible": True},
            }

    monkeypatch.setattr(api, "store", FakeStore())
    client = TestClient(api.app)
    response = client.get("/v1/tasks/legacy/results?smooth=true&overlay=-1")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    assert sorted(body) == ["2", "5"]
    assert body["2"]["scene_id"] == "legacy"
    assert body["2"]["smooth"] is True
    assert body["2"]["overlay_id"] == 777


def test_snapshot_uses_immutable_task_overlay_and_variant():
    store = PostgresStore.__new__(PostgresStore)
    calls = []
    store.get_meta = lambda task_id: {
        "id": task_id,
        "scene_id": "scene-1",
        "initial_variant": "smooth",
        "analysis_variant": "smooth",
        "analysis_overlay_id": 901,
    }
    store.get_n_statuses = lambda task_id, **kw: calls.append(("n", kw)) or {"1": {"status": "done"}}
    store.get_plan = lambda task_id, **kw: calls.append(("plan", kw)) or {"order": [1]}
    store.get_result_metas = lambda task_id, **kw: calls.append(("results", kw)) or {"1": {"n": 1}}

    snapshot = PostgresStore.snapshot(store, "task-1")

    assert snapshot["scene_id"] == "scene-1"
    assert snapshot["smooth"] is True
    assert snapshot["overlay_id"] == 901
    assert all(call[1] == {"variant": "smooth", "overlay_id": 901} for call in calls)


def test_snapshot_for_migrated_legacy_task_defaults_to_raw_zero():
    store = PostgresStore.__new__(PostgresStore)
    calls = []
    store.get_meta = lambda task_id: {
        "id": task_id,
        "scene_id": task_id,
        "initial_variant": "smooth",
        "analysis_variant": "raw",
        "analysis_overlay_id": 0,
    }
    store.get_n_statuses = lambda task_id, **kw: calls.append(kw) or {}
    store.get_plan = lambda task_id, **kw: calls.append(kw) or {}
    store.get_result_metas = lambda task_id, **kw: calls.append(kw) or {}

    snapshot = PostgresStore.snapshot(store, "legacy")

    assert snapshot["smooth"] is False
    assert snapshot["overlay_id"] == 0
    assert all(call == {"variant": "raw", "overlay_id": 0} for call in calls)


def test_ws_command_accepts_relative_overlay_and_new_task_context_wins(monkeypatch):
    assert WsCommand.model_validate({"action": "add", "n": [2], "overlay": -1}).overlay == -1

    scheduled = []
    published = []

    class FakeStore:
        def get_meta(self, task_id):
            return {
                "scene_id": "scene-new",
                "analysis_variant": "smooth",
                "analysis_overlay_id": 900,
                "initial_variant": "smooth",
            }

        def publish_event(self, task_id, event_type, payload, *, overlay_id=0):
            published.append((task_id, event_type, dict(payload), overlay_id))

    class FakeWorkflow:
        def schedule_requested_for_all(self, task_id, ns, *, smooth=False, overlay_id=0):
            scheduled.append((task_id, list(ns), smooth, overlay_id))
            return {"0": list(ns)}

    monkeypatch.setattr(api, "store", FakeStore())
    monkeypatch.setattr(api, "workflow", FakeWorkflow())

    reply = asyncio.run(
        api._ws_command(
            "task-new",
            {"action": "add", "n": [2], "smooth": False, "overlay": -1},
        )
    )

    assert reply == {"type": "ack", "action": "add"}
    assert scheduled == [("task-new", [2], True, 900)]
    assert published[0][2]["overlay_id"] == 900
    assert published[0][3] == 900


def test_pause_returns_plan_for_immutable_task_context(monkeypatch):
    seen = []

    class FakeStore:
        def get_meta(self, task_id):
            return {
                "scene_id": "scene-new",
                "analysis_variant": "smooth",
                "analysis_overlay_id": 900,
                "initial_variant": "smooth",
            }

        def set_paused(self, task_id, paused):
            seen.append(("pause", task_id, paused))

        def get_plan(self, task_id, *, variant, overlay_id):
            seen.append(("plan", task_id, variant, overlay_id))
            return {"order": [1, 2], "variant": variant, "overlay_id": overlay_id}

    monkeypatch.setattr(api, "store", FakeStore())
    client = TestClient(api.app)
    response = client.post("/v1/tasks/task-new/pause")
    assert response.status_code == 200
    assert response.json()["overlay_id"] == 900
    assert ("plan", "task-new", "smooth", 900) in seen
