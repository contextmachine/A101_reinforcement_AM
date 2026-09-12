"""Run the user's lattice-pool experiment (canon_pool_auto + exact selector) on one DXF and evaluate the
zones with the production layout/verification (same evaluator as the production runs).

Usage: PYTHONPATH=. python bench/exp_run.py <dxf> <axis> <K> <prod.json> <out.json> [--cap 50000] [--solver highs|cpsat]
       [--time-limit 600] [--workers 12]
"""
from __future__ import annotations

import argparse, json, sys, time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

EXP = Path("/Users/sthv/PycharmProjects/A101_reinforcement")
sys.path.insert(0, str(EXP)); sys.path.insert(0, str(EXP / "experiments"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agglo  # noqa: E402
import ilp  # noqa: E402
from A101.read_dxf import extract_polygons  # noqa: E402
from rebar_service.v2 import bars as bars_module  # noqa: E402
from evaluate import evaluate  # noqa: E402


def boxes_to_zones(eng, boxes: np.ndarray, *, axis: str, next_id: int = 1) -> list[dict]:
    """Experiment boxes (cell indices) -> production ZoneAdditional dicts, one per layer."""
    rows = eng.zone_rows(boxes)
    kg, rk = eng.cost(boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3])
    direction = bars_module._canonical_direction(axis)
    cross, long = bars_module._frame(axis)
    sign_cross = direction[cross]
    sign_long = bars_module._bar_direction(direction)[long]
    zones = []
    for row, r in zip(rows, rk):
        d, step, layers = float(eng.diam[r]), float(eng.step[r]), int(eng.layers[r])
        n_bars = max(2, int(round(row["width_mm"] / step)) + 1)
        x0, y0, x1, y1 = row["x0"], row["y0"], row["x1"], row["y1"]
        lo = (x0, y0); hi = (x1, y1)
        c_center = (lo[cross] + hi[cross]) / 2.0
        c_first, c_last = c_center - (n_bars - 1) * step / 2.0, c_center + (n_bars - 1) * step / 2.0
        origin = [0.0, 0.0]
        origin[cross] = c_first if sign_cross > 0 else c_last
        origin[long] = lo[long] if sign_long > 0 else hi[long]
        length = hi[long] - lo[long]
        for _ in range(layers):
            zones.append({"id": next_id, "kind": "additional", "arm": {"d": d, "step": step}, "left": 0,
                          "right": n_bars - 1, "length": float(length), "anchorage": None,
                          "origin": [float(origin[0]), float(origin[1])],
                          "direction": [float(direction[0]), float(direction[1])]})
            next_id += 1
    return zones


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dxf"); ap.add_argument("axis"); ap.add_argument("k", type=int); ap.add_argument("prod"); ap.add_argument("out")
    ap.add_argument("--cap", type=int, default=50_000); ap.add_argument("--solver", default="highs")
    ap.add_argument("--time-limit", type=float, default=600.0); ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--mip-gap", type=float, default=0.002); ap.add_argument("--lattice", type=int, default=0)
    a = ap.parse_args()
    args = SimpleNamespace(cell=100.0, isolated_fe=1, min_width_fe=2, anch_d=agglo.ANCH_D, lattice=a.lattice, cap=a.cap,
                           solver=a.solver, time_limit=a.time_limit, mip_gap=a.mip_gap, max_nnz=20_000_000,
                           workers=a.workers, verbose=False, polish_time=20.0, harvest_all=False)
    t0 = time.perf_counter()
    eng = ilp.load_engine(Path(a.dxf), args)
    t_load = time.perf_counter() - t0
    pool, L = ilp.canon_pool_auto(eng, args.lattice, args.cap)
    t_pool = time.perf_counter() - t0 - t_load
    hint = None
    coarse_log = []
    for coarse in (4 * L, 2 * L):  # nested-lattice warm start exactly as ilp.main does
        cp = ilp.canon_pool(eng, coarse, args.cap)
        rr = ilp.solve(eng, cp, a.k, args, hint, time_limit=args.time_limit * 0.25)
        if rr["boxes"] is not None:
            hint = rr["boxes"]
            coarse_log.append({"lattice": coarse, "pool": int(len(cp)), "mass": rr["mass"], "status": rr["status"], "t": round(rr["t_solve"], 1)})
    cand = pool if hint is None else np.unique(np.concatenate([pool, hint]), axis=0)
    r = ilp.solve(eng, cand, a.k, args, hint)
    t_total = time.perf_counter() - t0
    result = {"dxf": a.dxf, "axis": a.axis, "k": a.k, "cap": a.cap, "lattice": L, "pool": int(len(pool)), "cand": int(len(cand)),
              "grid": list(eng.need.shape), "cell_mm": eng.cell, "options": eng.labels,
              "t_load": round(t_load, 1), "t_pool": round(t_pool, 1), "t_total": round(t_total, 1),
              "coarse": coarse_log, "status": r["status"], "rows": r.get("rows"), "cols": r.get("cols"),
              "t_build": round(r.get("t_build", 0), 1), "t_solve": round(r.get("t_solve", 0), 1),
              "exp_mass_kg": r.get("mass"), "exp_bound_kg": r.get("bound")}
    if r["boxes"] is None:
        Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1)); print(json.dumps(result)); return
    boxes = np.asarray(r["boxes"], np.int64)
    result["zones_exp"] = eng.zone_rows(boxes)
    result["verify_cover"] = bool(eng.verify_cover(boxes))
    # production evaluator with the production's own background zone for this scene
    prod = json.load(open(a.prod))
    prod_zones = next(iter(prod["ns"].values()))["zones"]
    bg = next(z for z in prod_zones if z["kind"] == "bg")
    zones = [dict(bg)] + boxes_to_zones(eng, boxes, axis=a.axis)
    polys = extract_polygons(a.dxf)
    rows = [{"idx": i, "points": [[float(x), float(y)] for x, y in p["points"]], "load": float(p["load"]), "color": int(p["color"])}
            for i, p in enumerate(polys)]
    result["bg_zone"] = bg
    result["zones_prod_format"] = len(zones) - 1
    result["zones_prod"] = zones
    result["evaluation"] = evaluate(rows, zones, axis=a.axis)
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1, default=float))
    print(json.dumps({k: v for k, v in result.items() if k not in ("zones_exp", "options", "coarse")}, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
