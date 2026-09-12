"""Run the production v2 pipeline in-process on one DXF (same code path as the workers).

Usage: PYTHONPATH=. python bench/prod_run.py <dxf> <axis> <out.json> --ns 10,20 [--highs '{"presolve":"off"}'] [--threads 12] [--prepare-only]
"""
from __future__ import annotations

import argparse, json, sys, time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from rebar_service.config import Settings
from rebar_service.overlays import resolve_overlay
from rebar_service.v2.pipeline import V2Pipeline
from support.memory_v2_store import MemoryV2Store
from A101.read_dxf import extract_polygons


class FakeQueue:
    def __init__(self): self.jobs = deque()
    def enqueue_pipeline_job(self, job): self.jobs.append(dict(job)); return True


class FakeStore:
    def __init__(self, settings, rows):
        self.settings, self.rows, self.events = settings, rows, []
        self.v2, self.queue = MemoryV2Store(), FakeQueue()
        self.bars_queue = self.verification_queue = FakeQueue()
    def enqueue_pipeline_job(self, job): return self.queue.enqueue_pipeline_job(job)
    def resolved_scene_polygons(self, scene_id, *, variant="raw", overlay_id=0):
        return resolve_overlay(self.rows, self.events, overlay_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dxf"); ap.add_argument("axis"); ap.add_argument("out")
    ap.add_argument("--ns", default="10"); ap.add_argument("--highs", default="")
    ap.add_argument("--threads", type=int, default=12); ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--time-limit", type=float, default=None)
    ap.add_argument("--lattice-cap", type=int, default=0, help="REBAR_CANDIDATE_LATTICE_CAP (0 = exhaustive)")
    a = ap.parse_args()
    ns = [int(x) for x in a.ns.split(",") if x]
    polys = extract_polygons(a.dxf)
    rows = [{"idx": i, "points": [[float(x), float(y)] for x, y in p["points"]], "load": float(p["load"]),
             "color": int(p["color"])} for i, p in enumerate(polys)]
    log_dir = Path(a.out).with_suffix("") .as_posix() + "_logs"
    settings = Settings(
        solver_backend="highs", fit_milp_backend="auto", grid_size=300, fill_notches=1000, short_edge=300,
        simplify_step=1000, use_mosaic=True, min_internal_step=100, solver_threads=a.threads, fit_threads=1,
        solver_log_dir=log_dir, highs_options=a.highs, max_n=1000, require_optimal=True,
        solver_time_limit=a.time_limit, candidate_lattice_cap=a.lattice_cap,
    )
    store = FakeStore(settings, rows)
    pipeline = V2Pipeline(store, settings)
    config = {"axis": a.axis, "anchor_factor": 40.0, "back_grid": None, "stock": None, "max_layers": 2,
              "min_width_mm": 1000.0, "max_snap_mm": 600.0, "min_bar_gap_mm": None,
              "steel_density_kg_m3": 7850.0, "solver": {"solver_time_limit": a.time_limit}}
    t0 = time.perf_counter()
    task_id = pipeline.create_task(scene_id="scene", overlay_id=0, smooth=False, config=config, ns=ns)
    stage_times: dict[str, float] = {}
    while store.queue.jobs:
        job = store.queue.jobs.popleft()
        kind = job["kind"]; n = job.get("n") if job.get("n") is not None else (job.get("payload") or {}).get("n")
        t = time.perf_counter()
        pipeline.dispatch(job, "bench")
        stage_times[f"{kind}:{n}"] = round(time.perf_counter() - t, 1)
        print(f"  {kind} n={n} {stage_times[f'{kind}:{n}']}s", flush=True)
        if a.prepare_only and kind == "v2_prepare":
            break
    total = time.perf_counter() - t0
    task = store.v2.get_task(task_id)
    result = {"dxf": a.dxf, "axis": a.axis, "highs": a.highs, "threads": a.threads, "lattice_cap": a.lattice_cap, "total_s": round(total, 1),
              "stages": stage_times, "task_state": task["state"], "max_useful_n": task.get("max_useful_n"),
              "min_useful_n": task.get("min_useful_n"), "prepare_info": task.get("prepare_info"),
              "polygons": len(rows), "ns": {}}
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from evaluate import evaluate
    for n in ns:
        row = store.v2.get_n(task_id, n) or {}
        mm = row.get("mass_metrics") or {}
        res = row.get("result") or {}
        if res.get("bars"):
            result.setdefault("evaluation", {})[n] = evaluate(rows, axis=a.axis, bars=res["bars"], mass_metrics=mm)
            # diagnostic: re-laying out the returned zones (what POST /v2/bars would do)
            result.setdefault("roundtrip", {})[n] = evaluate(rows, res["zones"], axis=a.axis)
        result["ns"][n] = {"state": row.get("state"), "status": row.get("status"), "fun": row.get("fun"),
                           "error": row.get("error"), "mass_metrics": mm,
                           "zones": (row.get("result") or {}).get("zones"), "bars": len((row.get("result") or {}).get("bars") or [])}
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k not in ("ns", "prepare_info")}, ensure_ascii=False))
    for n, r in result["ns"].items():
        add = (r["mass_metrics"] or {}).get("additional") or {}
        print(f"  N={n}: {r['state']}/{r['status']} fun={r['fun']} additional with_anchorage_kg={add.get('with_anchorage_kg')} bars={r['bars']} err={r['error']}")


if __name__ == "__main__":
    main()
