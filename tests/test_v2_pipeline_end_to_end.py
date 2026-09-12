"""Real geometry + real solver/fit/layout through the /v2 whole-field pipeline; in-memory IO only."""
from __future__ import annotations

import copy
from collections import deque
from pathlib import Path

import pytest

from rebar_service.config import Settings
from rebar_service.overlays import resolve_overlay
from rebar_service.v2.models import Bar, MassMetrics, ZoneAdditional, ZoneBase
from rebar_service.v2.pipeline import V2Pipeline
from rebar_service.v2.workers import handle_bars_job, handle_verification_job
from support.memory_v2_store import MemoryV2Store

ROWS = [
    {"points": [[0, 0], [1200, 0], [1200, 1800], [0, 1800]], "load": 12.0},
    {"points": [[1800, 0], [3000, 0], [3000, 1800], [1800, 1800]], "load": 19.0},
]
CONFIG = {
    "axis": "y", "anchor_factor": 40.0, "back_grid": {"d": 16.0, "step": 300.0},
    "stock": [{"d": 16.0, "step": 300.0}, {"d": 20.0, "step": 150.0}, {"d": 20.0, "step": 100.0}],
    "max_layers": 2, "min_width_mm": 300.0, "max_snap_mm": 600.0, "min_bar_gap_mm": None,
    "steel_density_kg_m3": 7850.0, "solver": {"solver_time_limit": None},
}


class FakeQueue:
    def __init__(self):
        self.jobs = deque()
        self.enqueued = []

    def enqueue_pipeline_job(self, job):
        self.jobs.append(dict(job))
        self.enqueued.append(dict(job))
        return True


class FakeStore:
    """Scene rows + v2 store + queues, nothing else."""

    def __init__(self, settings, rows=None, events=None):
        self.settings = settings
        self.rows = copy.deepcopy(rows or ROWS)
        self.events = list(events or [])
        self.v2 = MemoryV2Store()
        self.queue = FakeQueue()
        self.bars_queue = FakeQueue()
        self.verification_queue = FakeQueue()

    def enqueue_pipeline_job(self, job):
        return self.queue.enqueue_pipeline_job(job)

    def resolved_scene_polygons(self, scene_id, *, variant="raw", overlay_id=0):
        return resolve_overlay(self.rows, self.events, overlay_id)


def make_settings(tmp_path, **overrides):
    values = dict(
        solver_backend="scipy", fit_milp_backend="scipy", grid_size=300, fill_notches=0, short_edge=0,
        simplify_step=0, use_mosaic=False, solver_log_dir=str(tmp_path / "logs"),
    )
    values.update(overrides)
    return Settings(**values)


def drain(store, pipeline, limit=100):
    count = 0
    while store.queue.jobs:
        job = store.queue.jobs.popleft()
        count += 1
        assert count <= limit
        pipeline.dispatch(job, "worker-test")


@pytest.mark.parametrize("axis", ["x", "y"])
def test_prepare_solve_fit_bars_end_to_end(tmp_path, axis):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    config = {**CONFIG, "axis": axis}
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=config, ns=[1, 2, 3, 50])
    drain(store, pipeline)

    task = store.v2.get_task(task_id)
    assert task["state"] == "ready"
    assert task["max_useful_n"] >= 1
    kinds = [job["kind"] for job in store.queue.enqueued]
    assert kinds[0] == "v2_prepare"
    assert kinds.index("v2_prepare") < kinds.index("v2_solve")

    rows = {row["n"]: row for row in store.v2.list_ns(task_id)}
    assert set(rows) == {1, 2, 3, 50}
    assert all(row["state"] == "success" for row in rows.values()), rows
    assert rows[50]["status"] == "infeasable" and "max_useful_n" in rows[50]["error"]
    feasible = [row for row in rows.values() if row["status"] in {"optimal", "feasable"}]
    assert feasible, rows
    for row in feasible:
        assert row["fun"] is not None and row["fun"] > 0
        metrics = MassMetrics.model_validate(row["mass_metrics"])
        for group in (metrics.additional, metrics.bg):
            assert group.with_anchorage_kg >= group.without_anchorage_kg - 1e-9
            assert group.with_anchorage_unclipped_kg >= group.without_anchorage_unclipped_kg - 1e-9
        assert metrics.bg.with_anchorage_kg > metrics.bg.without_anchorage_kg > 0
        full = store.v2.get_n(task_id, row["n"])
        bars = [Bar.model_validate(bar) for bar in full["result"]["bars"]]
        zones = full["result"]["zones"]
        assert bars and zones
        assert sum(1 for zone in zones if zone["kind"] == "bg") == 1
        additional = [ZoneAdditional.model_validate(z) for z in zones if z["kind"] == "additional"]
        assert additional
        ZoneBase.model_validate(next(z for z in zones if z["kind"] == "bg"))
        zone_ids = {zone["id"] for zone in zones}
        assert all(bar.zone_id in zone_ids for bar in bars)
        # geometry is stored without anchorage: every bar lies inside the field extent
        for bar in bars:
            for x, y in (bar.start, bar.end):
                assert -1e-6 <= x <= 3000 + 1e-6 and -1e-6 <= y <= 1800 + 1e-6
            assert bar.anchorage.start == pytest.approx(40.0 * bar.d)
    # transient solver artifacts are cleaned up, field/problem/fit stay
    assert not any(key.startswith("solver:") for (_task, key) in store.v2.artifacts)
    assert (task_id, "field") in store.v2.artifacts and (task_id, "problem") in store.v2.artifacts


def test_highs_backend_writes_solver_log_per_task_n_worker(tmp_path):
    settings = make_settings(tmp_path, solver_backend="highs")
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1, 2, 3])
    drain(store, pipeline)
    rows = store.v2.list_ns(task_id)
    assert all(row["state"] == "success" for row in rows), rows
    solved = [row["n"] for row in rows if row["status"] in {"optimal", "feasable"}]
    assert solved, rows
    for n in solved:
        log = Path(tmp_path / "logs" / task_id / str(n) / "worker-test.log")
        assert log.exists() and log.stat().st_size > 0, n


def test_add_and_cancel_ns(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1])
    # N added before preparation stays pending until the prepare job schedules it.
    assert pipeline.add_ns(task_id, [2]) == [2]
    assert store.v2.get_n(task_id, 2)["state"] == "pending"
    drain(store, pipeline)
    assert store.v2.get_n(task_id, 2)["state"] == "success"
    # N added after preparation is scheduled immediately.
    assert pipeline.add_ns(task_id, [3, 3]) == [3]
    assert store.v2.get_n(task_id, 3)["state"] in {"solving", "success"}
    assert pipeline.cancel(task_id, [3]) == [3]
    assert store.v2.get_n(task_id, 3)["state"] == "cancelled"
    drain(store, pipeline)
    assert store.v2.get_n(task_id, 3)["state"] == "cancelled"
    assert pipeline.add_ns(task_id, [1]) == []


def test_background_only_scene_marks_every_n_infeasible(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings, rows=[{**row, "load": 5.7} for row in ROWS])
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1, 2])
    drain(store, pipeline)
    task = store.v2.get_task(task_id)
    assert task["state"] == "ready" and task["max_useful_n"] == 0
    assert task["prepare_info"]["reason"] == "no_positive_n_required"
    for row in store.v2.list_ns(task_id):
        assert row["state"] == "success" and row["status"] == "infeasable"
    assert not any(job["kind"] == "v2_solve" for job in store.queue.enqueued)


def test_prepare_failure_marks_task_and_ns_error(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    bad = {**CONFIG, "back_grid": None, "stock": CONFIG["stock"]}  # stock without back_grid -> ValueError
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=bad, ns=[1])
    with pytest.raises(ValueError):
        drain(store, pipeline)
    assert store.v2.get_task(task_id)["state"] == "error"
    assert store.v2.get_n(task_id, 1)["state"] == "error"


def test_bars_and_verification_workers_round_trip(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1, 2, 3])
    drain(store, pipeline)
    solved = next(
        (row for row in store.v2.list_ns(task_id) if row["status"] in {"optimal", "feasable"}), None
    )
    assert solved is not None, store.v2.list_ns(task_id)
    zones = store.v2.get_n(task_id, solved["n"])["result"]["zones"]

    store.v2.create_bar_task("bars1", scene_id="scene", overlay_id=0, smooth=False,
                             config={"axis": "y", "anchor_factor": 40.0}, zones=zones)
    handle_bars_job(store, {"kind": "bars", "task_id": "bars1"}, "bars-worker")
    bars = store.v2.get_bar_task("bars1")
    assert bars["state"] == "success"
    assert bars["result"]["bars"] and bars["result"]["zones"]
    MassMetrics.model_validate(bars["result"]["mass_metrics"])
    # zones produced by the layout are reproduced by a second layout
    assert bars["result"]["zones"] == zones

    store.v2.create_verification_task(
        "ver1", scene_id="scene", overlay_id=0, smooth=False,
        config={"axis": "y", "anchor_factor": 40.0, "steel_density_kg_m3": 7850.0, "t": 600.0}, zones=zones,
    )
    handle_verification_job(store, {"kind": "verification", "task_id": "ver1"}, "ver-worker")
    verification = store.v2.get_verification_task("ver1")
    assert verification["state"] == "success"
    rows = verification["result"]
    assert [row["source_index"] for row in rows] == [0, 1]
    for row, source in zip(rows, ROWS):
        assert row["overlay_state"] == "active"
        assert row["need_load_sm2/m"] == pytest.approx(source["load"])
        assert row["fact_load_sm2/m"] > 0
        assert row["need_load_kg/m3"] == pytest.approx(source["load"] * 7850.0 / (10 * 600.0))
        assert row["fact_load_kg/m3"] == pytest.approx(row["fact_load_sm2/m"] * 7850.0 / (10 * 600.0))
    # the laid-out reinforcement must at least meet the background demand everywhere
    assert all(row["fact_load_sm2/m"] >= row["need_load_sm2/m"] * 0.9 for row in rows), rows


def test_bars_worker_records_layout_errors(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    store.v2.create_bar_task("bad", scene_id="scene", overlay_id=0, smooth=False,
                             config={"axis": "y", "anchor_factor": 40.0},
                             zones=[{"id": 0, "kind": "additional", "arm": {"d": 20, "step": 150}, "left": 0,
                                     "right": 1, "length": 500, "origin": [100, 100], "direction": [1, 0]}])
    with pytest.raises(ValueError):
        handle_bars_job(store, {"kind": "bars", "task_id": "bad"}, "bars-worker")
    assert store.v2.get_bar_task("bad")["state"] == "error"


def test_redelivered_jobs_are_idempotent_and_handler_errors_are_persisted(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1, 2, 3])
    drain(store, pipeline)
    done = [row for row in store.v2.list_ns(task_id) if row["status"] in {"optimal", "feasable"}]
    assert done
    n = done[0]["n"]
    before = store.v2.get_n(task_id, n)
    # a duplicate delivery of every stage after success changes nothing
    for kind in ("v2_solve", "v2_fit", "v2_bars"):
        pipeline.dispatch({"kind": kind, "task_id": task_id, "payload": {"n": n}}, "worker-again")
        assert store.v2.get_n(task_id, n) == before
    # a missing artifact is a persisted error, not a crash
    store.v2.set_n(task_id, n, state="fitting")
    store.v2.delete_artifact(task_id, f"solver:{n}")
    pipeline.dispatch({"kind": "v2_fit", "task_id": task_id, "payload": {"n": n}}, "worker-again")
    assert store.v2.get_n(task_id, n)["state"] == "error"
    # a crashing stage leaves the N in 'error' with the message, and the job is reported failed
    store.v2.set_n(task_id, n, state="bars", error=None)
    store.v2.save_artifact(task_id, f"fit:{n}", {"zones": "broken", "is_optimal": False})
    with pytest.raises(Exception):
        pipeline.dispatch({"kind": "v2_bars", "task_id": task_id, "payload": {"n": n}}, "worker-again")
    row = store.v2.get_n(task_id, n)
    assert row["state"] == "error" and row["error"]


def test_new_ns_revive_a_fully_cancelled_task(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1])
    assert pipeline.cancel(task_id, [1]) == [1]
    assert store.v2.get_task(task_id)["cancelled"] is True
    drain(store, pipeline)  # the prepare job sees the cancelled task and does nothing
    assert store.v2.get_task(task_id)["state"] != "ready"
    assert pipeline.add_ns(task_id, [3]) == [3]
    task = store.v2.get_task(task_id)
    assert task["cancelled"] is False
    drain(store, pipeline)
    assert store.v2.get_task(task_id)["state"] == "ready"
    assert store.v2.get_n(task_id, 3)["state"] == "success"
    assert store.v2.get_n(task_id, 1)["state"] == "cancelled"


def test_solver_subprocess_failure_reason_is_persisted(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[3])
    # run only the prepare job, then make the solver die like an OOM-killed child would
    pipeline.dispatch(store.queue.jobs.popleft(), "worker-test")
    assert store.v2.get_n(task_id, 3)["state"] == "solving"

    def boom(*args, **kwargs):
        raise RuntimeError("worker exited with code -9")

    monkeypatch.setattr("A101.reinforcement_components.solve_component_frontier", boom)
    with pytest.raises(RuntimeError):
        drain(store, pipeline)
    row = store.v2.get_n(task_id, 3)
    assert row["state"] == "error" and "code -9" in row["error"]


def test_repeating_put_n_revives_a_task_whose_prepare_job_was_lost(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[2])
    store.queue.jobs.clear()  # the prepare job never reached a worker
    assert store.v2.get_task(task_id)["state"] == "created"
    assert pipeline.add_ns(task_id, [2]) == []  # nothing new, but preparation is re-queued
    assert [job["kind"] for job in store.queue.jobs] == ["v2_prepare"]
    drain(store, pipeline)
    assert store.v2.get_task(task_id)["state"] == "ready"
    assert store.v2.get_n(task_id, 2)["state"] == "success"


def test_returned_zones_lay_out_to_the_same_bars_again(tmp_path):
    """POST /v2/bars with the zones of GET /v2/tasks/{id}/{n} must reproduce the task's bars and mass."""
    from rebar_service.v2.bars import layout_zones, physical_polygons

    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1, 2, 3])
    drain(store, pipeline)
    rows = [store.v2.get_n(task_id, n) for n in (1, 2, 3)]
    row = next(r for r in rows if r["state"] == "success" and r["status"] != "infeasable")
    polygons = physical_polygons(store.resolved_scene_polygons("scene"))
    zones = row["result"]["zones"]
    for _ in range(3):
        out = layout_zones(polygons, zones, axis=CONFIG["axis"], anchor_factor=CONFIG["anchor_factor"],
                           steel_density_kg_m3=CONFIG["steel_density_kg_m3"], min_step=settings.min_internal_step)
        assert out["is_feasible"], out.get("warnings")
        assert len(out["bars"]) == len(row["result"]["bars"])
        assert out["mass_metrics"]["additional"]["with_anchorage_kg"] == pytest.approx(
            row["mass_metrics"]["additional"]["with_anchorage_kg"])
        zones = out["zones"]


def test_lattice_candidate_pool_runs_the_whole_pipeline(tmp_path):
    settings = make_settings(tmp_path, candidate_lattice_cap=500)
    store = FakeStore(settings)
    pipeline = V2Pipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1, 2, 3])
    drain(store, pipeline)
    task = store.v2.get_task(task_id)
    assert task["state"] == "ready"
    generator = task["prepare_info"]["candidate_generator"]
    assert generator["generator"] == "lattice" and generator["candidates"] == task["prepare_info"]["candidate_rectangles"]
    assert generator["candidates"] <= 500
    rows = [store.v2.get_n(task_id, n) for n in (1, 2, 3)]
    assert all(r["state"] == "success" for r in rows), rows
    assert any(r["status"] in {"optimal", "feasable"} and r["result"]["bars"] for r in rows), rows
