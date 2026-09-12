"""Alternative solver (vendored experiments) behind the v2 task protocol.

CP-SAT (ortools) and highspy cannot share one process (both ship a libhighs with the same
soname), so everything that needs the solver runs in a child interpreter; only the pure
scene-to-mosaic adapter is tested in-process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_in_child(body: str) -> dict:
    script = textwrap.dedent('''
        import json, sys
        from pathlib import Path
        sys.path.insert(0, str(Path("tests")))
        from ortools.sat.python import cp_model  # noqa: F401  (load CP-SAT before anything else)
        from test_v2_pipeline_end_to_end import CONFIG, ROWS, FakeStore, drain, make_settings
        from rebar_service.altsolver import build_engine, solve_n
        from rebar_service.altsolver.pipeline import AltSolverPipeline
        from rebar_service.v2.models import MassMetrics
    ''') + textwrap.dedent(body)
    proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_mosaic_from_rows_uses_legend_convention_and_drops_removed_polygons():
    from rebar_service.altsolver.engine import mosaic_from_rows

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


def test_engine_and_selection_on_the_tiny_scene():
    out = _run_in_child('''
        settings = make_settings(Path("/tmp"))
        scene = build_engine(ROWS, config=CONFIG, settings=settings)
        solution = solve_n(scene, 2, settings=settings, time_limit=30)
        print(json.dumps({"background": list(scene.background), "demand": scene.info["demand_cells"],
                          "solver": scene.info["solver"], "status": solution.status,
                          "kinds": [z["kind"] for z in solution.zones],
                          "runs_ok": all(z["left"] == 0 and z["right"] >= 1 for z in solution.zones[1:])}))
    ''')
    assert out["background"] == [16.0, 300.0]
    assert out["demand"] > 0 and out["solver"] == "canon_pool_cpsat"
    assert out["status"] in {"OPTIMAL", "FEASIBLE", "GAP"}
    # K=2 boxes; a two-layer rung of the user stock is emitted as stacked zones, so up to 2*K zones
    assert out["kinds"][0] == "bg" and 1 <= len(out["kinds"]) - 1 <= 4 and out["runs_ok"]


def test_alt_pipeline_runs_prepare_and_solve_end_to_end(tmp_path):
    out = _run_in_child(f'''
        settings = make_settings(Path({str(tmp_path)!r}))
        store = FakeStore(settings)
        pipeline = AltSolverPipeline(store, settings)
        task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=CONFIG, ns=[1, 2, 50])
        drain(store, pipeline)
        task = store.v2.get_task(task_id)
        rows = {{n: store.v2.get_n(task_id, n) for n in (1, 2, 50)}}
        solved = [r for r in rows.values() if r["status"] in {{"optimal", "feasable"}}]
        for r in solved:
            MassMetrics.model_validate(r["mass_metrics"])
        print(json.dumps({{"state": task["state"], "solver": task["prepare_info"]["solver"], "max_n": task["max_useful_n"],
                           "min_n": task["min_useful_n"], "above": rows[50]["status"], "above_error": rows[50]["error"],
                           "states": [r["state"] for r in rows.values()], "solved": len(solved),
                           "bars": [len(r["result"]["bars"]) for r in solved],
                           "bg_first": all(r["result"]["zones"][0]["kind"] == "bg" for r in solved),
                           "statuses": [r["result"]["solver"]["status"] for r in solved],
                           "fun_pos": all(r["fun"] is not None and r["fun"] > 0 for r in solved),
                           "kinds": sorted({{j["kind"] for j in store.queue.enqueued}})}}))
    ''')
    assert out["state"] == "ready" and out["solver"] == "canon_pool_cpsat"
    # exact useful range: the unconstrained minimum-mass cover of the tiny scene uses 1 or 2 boxes
    assert out["min_n"] == 1 and 1 <= out["max_n"] <= 2
    assert out["above"] == "infeasable" and "max_useful_n" in out["above_error"]
    assert all(s == "success" for s in out["states"]) and out["solved"] >= 1
    assert all(b > 0 for b in out["bars"]) and out["bg_first"] and out["fun_pos"]
    assert all(s in {"OPTIMAL", "FEASIBLE", "GAP"} for s in out["statuses"])
    assert set(out["kinds"]) <= {"v2_prepare", "v2_solve"}
