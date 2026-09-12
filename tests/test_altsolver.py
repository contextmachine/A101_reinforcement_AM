"""Alternative solver (vendored experiments) behind the v2 task protocol."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v2_pipeline_end_to_end import CONFIG, ROWS, FakeStore, drain, make_settings  # noqa: E402

from rebar_service.altsolver import build_engine, solve_n  # noqa: E402

try:  # the CP-SAT extension cannot load next to highspy's libhighs on macOS (same dylib install name)
    from ortools.sat.python import cp_model  # noqa: F401
    CPSAT_LOADS = True
except ImportError:
    CPSAT_LOADS = False
needs_cpsat = pytest.mark.skipif(not CPSAT_LOADS, reason="ortools CP-SAT cannot be loaded in this process (libhighs clash)")
from rebar_service.altsolver.engine import mosaic_from_rows  # noqa: E402
from rebar_service.altsolver.pipeline import AltSolverPipeline  # noqa: E402
from rebar_service.v2.models import MassMetrics  # noqa: E402


def test_mosaic_from_rows_uses_legend_convention_and_drops_removed_polygons():
    rows = [
        {"points": [[0, 0], [1200, 0], [1200, 1800], [0, 1800]], "load": 12.0},
        {"points": [[1800, 0], [3000, 0], [3000, 1800], [1800, 1800]], "load": 19.0, "overlay_state": "active"},
        {"points": [[3000, 0], [3600, 0], [3600, 1800], [3000, 1800]], "load": 30.0, "overlay_state": "background_only"},
        {"points": [[3600, 0], [4200, 0], [4200, 1800], [3600, 1800]], "load": 30.0, "overlay_state": "removed"},
        {"points": [[0, 1800], [600, 1800], [300, 2400]], "load": 12.0},  # a triangle
    ]
    mosaic = mosaic_from_rows(rows, axis="y", background_area=6.7)
    assert mosaic.polygons.shape == (4, 4, 2)
    assert list(mosaic.required) == [13.0, 20.0, 6.7, 13.0]
    assert [b[2] for b in mosaic.scale] == [6.7, 13.0, 20.0]
    assert mosaic.direction == "Y"


@needs_cpsat
def test_engine_and_selection_on_the_tiny_scene():
    settings = make_settings(Path("/tmp"))
    scene = build_engine(ROWS, config=CONFIG, settings=settings)
    assert scene.background == (16.0, 300.0)
    assert scene.info["demand_cells"] > 0 and scene.info["solver"] == "canon_pool_cpsat"
    solution = solve_n(scene, 2, settings=settings, time_limit=30)
    assert solution.status in {"OPTIMAL", "FEASIBLE", "GAP"}
    kinds = [z["kind"] for z in solution.zones]
    assert kinds[0] == "bg" and 1 <= len(kinds) - 1 <= 2
    assert all(z["left"] == 0 and z["right"] >= 1 for z in solution.zones[1:])


@needs_cpsat
def test_alt_pipeline_runs_prepare_and_solve_end_to_end(tmp_path):
    settings = make_settings(tmp_path)
    store = FakeStore(settings)
    pipeline = AltSolverPipeline(store, settings)
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1, 2])
    drain(store, pipeline)
    task = store.v2.get_task(task_id)
    assert task["state"] == "ready" and task["prepare_info"]["solver"] == "canon_pool_cpsat"
    assert task["max_useful_n"] >= 2
    rows = {n: store.v2.get_n(task_id, n) for n in (1, 2)}
    assert all(r["state"] == "success" for r in rows.values()), rows
    solved = [r for r in rows.values() if r["status"] in {"optimal", "feasable"}]
    assert solved, rows
    for r in solved:
        MassMetrics.model_validate(r["mass_metrics"])
        assert r["result"]["bars"] and r["result"]["zones"][0]["kind"] == "bg"
        assert r["result"]["solver"]["status"] in {"OPTIMAL", "FEASIBLE", "GAP"}
        assert r["fun"] is not None and r["fun"] > 0
    # only prepare and solve jobs were needed: no fit/bars jobs on the queue
    assert {j["kind"] for j in store.queue.enqueued} <= {"v2_prepare", "v2_solve"}
