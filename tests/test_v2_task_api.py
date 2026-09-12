"""HTTP contract of the /v2 task, bars and verification routes (store and queues faked)."""
from __future__ import annotations

from collections import deque

import pytest
from fastapi.testclient import TestClient

import rebar_service.api as api
from rebar_service.overlays import resolve_overlay, resolve_overlay_selector
from support.memory_v2_store import MemoryV2Store

client = TestClient(api.app)
SQUARE = [[0.0, 0.0], [3000.0, 0.0], [3000.0, 1800.0], [0.0, 1800.0]]
ZONES = [
    {"id": 0, "kind": "bg", "arm": {"d": 16.0, "step": 300.0}},
    {"id": 1, "kind": "additional", "arm": {"d": 20.0, "step": 150.0}, "left": 1, "right": 1,
     "length": 1800.0, "origin": [1500.0, 0.0], "direction": [1.0, 0.0]},
]


class FakeQueue:
    def __init__(self):
        self.jobs = deque()

    def enqueue_pipeline_job(self, job):
        self.jobs.append(dict(job))
        return True


class FakeStore:
    def __init__(self, state="ready"):
        self.scenes = {"scene1": {"scene_id": "scene1", "state": state}}
        self.polygons = [{"points": SQUARE, "load": 12.0}]
        self.events = []
        self.v2 = MemoryV2Store()
        self.queue = FakeQueue()
        self.bars_queue = FakeQueue()
        self.verification_queue = FakeQueue()

    def get_scene(self, scene_id):
        return self.scenes.get(scene_id)

    def resolve_scene_overlay_id(self, scene_id, selector=0):
        if scene_id not in self.scenes:
            raise KeyError(scene_id)
        return resolve_overlay_selector(self.events, selector)

    def resolved_scene_polygons(self, scene_id, *, variant="raw", overlay_id=0):
        return resolve_overlay(self.polygons, self.events, overlay_id)

    def enqueue_pipeline_job(self, job):
        return self.queue.enqueue_pipeline_job(job)


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(api, "store", fake)
    return fake


def test_create_task_validates_scene_overlay_and_n(store, monkeypatch):
    body = {"scene_id": "scene1", "overlay_id": 0, "smooth": False, "n": [3, 1, 3], "config": {"axis": "x"}}
    response = client.put("/v2/tasks", json=body)
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    assert [job["kind"] for job in store.queue.jobs] == ["v2_prepare"]
    task = store.v2.get_task(task_id)
    assert task["scene_id"] == "scene1" and task["config"]["axis"] == "x"
    assert [row["n"] for row in store.v2.list_ns(task_id)] == [1, 3]

    assert client.put("/v2/tasks", json={**body, "scene_id": "nope"}).status_code == 404
    assert client.put("/v2/tasks", json={**body, "overlay_id": -1}).status_code == 404
    assert client.put("/v2/tasks", json={**body, "n": [0]}).status_code == 422
    assert client.put("/v2/tasks", json={**body, "n": [api.settings.max_n + 1]}).status_code == 422
    assert client.put("/v2/tasks", json={**body, "config": {"solver": {"threads": 4}}}).status_code == 422
    store.scenes["scene1"]["state"] = "preparing"
    assert client.put("/v2/tasks", json=body).status_code == 409


def test_task_views_add_and_cancel(store):
    task_id = client.put("/v2/tasks", json={"scene_id": "scene1", "n": [2]}).json()["task_id"]
    view = client.get(f"/v2/tasks/{task_id}").json()
    assert view["task_id"] == task_id and view["scene_id"] == "scene1"
    assert view["solutions"] == [{"n": 2, "state": "pending"}]
    assert client.get("/v2/tasks/missing").status_code == 404

    view = client.put(f"/v2/tasks/{task_id}/n", json={"n": [5, 2]}).json()
    assert [row["n"] for row in view["solutions"]] == [2, 5]
    assert client.put(f"/v2/tasks/{task_id}/n", json={"n": [api.settings.max_n + 1]}).status_code == 422

    view = client.put(f"/v2/tasks/{task_id}/cancel", json={"n": [5]}).json()
    assert {row["n"]: row["state"] for row in view["solutions"]} == {2: "pending", 5: "cancelled"}
    view = client.put(f"/v2/tasks/{task_id}/cancel").json()
    assert all(row["state"] == "cancelled" for row in view["solutions"])

    store.v2.set_n(task_id, 2, state="success", status="feasable", fun=12.5,
                   mass_metrics={"additional": {k: 1.0 for k in MASS}, "bg": {k: 2.0 for k in MASS}},
                   result={"bars": [BAR], "zones": ZONES_OUT})
    solution = client.get(f"/v2/tasks/{task_id}/2").json()
    assert solution["state"] == "success" and solution["status"] == "feasable" and solution["fun"] == 12.5
    assert solution["bars"] == [BAR] and solution["zones"][0]["kind"] == "bg"
    assert solution["mass_metrics"]["bg"]["with_anchorage_kg"] == 2.0
    assert client.get(f"/v2/tasks/{task_id}/99").status_code == 404
    summary = client.get(f"/v2/tasks/{task_id}").json()["solutions"][0]
    assert "bars" not in summary and summary["fun"] == 12.5


MASS = ("with_anchorage_kg", "without_anchorage_kg", "with_anchorage_unclipped_kg", "without_anchorage_unclipped_kg")
BAR = {"zone_id": 0, "start": [100.0, 0.0], "end": [100.0, 1800.0], "d": 16.0, "anchorage": {"start": 640.0, "end": 640.0}}
ZONES_OUT = [
    {"id": 0, "kind": "bg", "arm": {"d": 16.0, "step": 300.0}, "anchorage": {"start": 640.0, "end": 640.0}},
]


def test_bars_request_is_queued_on_the_isolated_queue(store):
    body = {"scene_id": "scene1", "smooth": False, "overlay_id": 0,
            "config": {"axis": "x", "anchor_factor": 40, "min_bar_gap_mm": 50}, "zones": ZONES}
    response = client.post("/v2/bars", json=body)
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    assert response.json()["state"] == "pending"
    assert [job["kind"] for job in store.bars_queue.jobs] == ["bars"]
    assert store.bars_queue.jobs[0]["task_id"] == task_id
    assert not store.queue.jobs and not store.verification_queue.jobs
    row = store.v2.get_bar_task(task_id)
    assert row["zones"][1]["kind"] == "additional" and row["config"]["axis"] == "x"

    assert client.get(f"/v2/bars/{task_id}").json() == {"task_id": task_id, "state": "pending"}
    store.v2.set_bar_task(task_id, state="success", result={"bars": [BAR], "zones": ZONES_OUT,
                                                              "mass_metrics": {"additional": {k: 0.0 for k in MASS},
                                                                               "bg": {k: 1.0 for k in MASS}}})
    view = client.get(f"/v2/bars/{task_id}").json()
    assert view["state"] == "success" and view["bars"] == [BAR] and view["zones"] == ZONES_OUT
    store.v2.set_bar_task(task_id, state="error", error="boom")
    assert client.get(f"/v2/bars/{task_id}").json() == {"task_id": task_id, "state": "error", "error": "boom"}
    assert client.get("/v2/bars/missing").status_code == 404

    two_bg = {**body, "zones": [ZONES[0], {**ZONES[0], "id": 7}]}
    assert client.post("/v2/bars", json=two_bg).status_code == 422
    no_bg = {**body, "zones": [ZONES[1]]}
    assert client.post("/v2/bars", json=no_bg).status_code == 422


def test_verification_request_is_queued_and_reported(store):
    body = {"scene_id": "scene1", "config": {"axis": "y", "anchor_factor": 40, "steel_density_kg_m3": 7850,
                                             "t": 600, "min_bar_gap_mm": 50}, "zones": ZONES}
    response = client.post("/v2/verification", json=body)
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    assert [job["kind"] for job in store.verification_queue.jobs] == ["verification"]
    assert store.v2.get_verification_task(task_id)["config"]["t"] == 600
    assert client.post("/v2/verification", json={**body, "config": {"axis": "y"}}).status_code == 422

    assert client.get(f"/v2/verification/{task_id}").json() == {"verification_id": task_id, "state": "pending"}
    rows = [{"source_index": 0, "overlay_state": "active", "need_load_sm2/m": 12.0, "fact_load_sm2/m": 13.4,
             "need_load_kg/m3": 15.7, "fact_load_kg/m3": 17.5}]
    store.v2.set_verification_task(task_id, state="success", result=rows)
    view = client.get(f"/v2/verification/{task_id}").json()
    assert view["state"] == "success" and view["result"] == rows
    assert client.get("/v2/verification/missing").status_code == 404
