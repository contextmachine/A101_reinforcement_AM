"""Prototype of a physically based verification: rods as a set of (axis, diameter); density at a point is the
nearest parallel rod's bar area over its tributary width (half-distance to actual neighbours, capped at the
crack-control reach r = 5(c + d/2)). Compares with the current cylinder-intersection metric on the benchmark rods.
"""
from __future__ import annotations

import json, sys
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon
from shapely import contains_xy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_like_endpoint import FakeStore  # noqa: E402
from A101.read_dxf import extract_polygons  # noqa: E402
from rebar_service.config import Settings  # noqa: E402
from rebar_service.v2.workers import handle_bars_job  # noqa: E402
from rebar_service.v2.verification import reinforcement_rows  # noqa: E402

COVER_MM = 30.0
CELL = 20.0  # raster resolution, mm


def density_raster(bars, axis, x0, y0, nx, ny):
    """Smeared reinforcement density (cm²/m) on a raster; bars run along `axis`."""
    dens = np.zeros((ny, nx), float)
    cross, long = (1, 0) if axis == "x" else (0, 1)   # index of the cross coordinate in a point
    # per bar: cross position, longitudinal extent, area, reach
    rods = []
    for b in bars:
        s, e = b["start"], b["end"]; d = float(b["d"])
        c = 0.5 * (s[cross] + e[cross]); l0, l1 = sorted((s[long], e[long]))
        rods.append((c, l0, l1, np.pi * d * d / 4.0, 5.0 * (COVER_MM + d / 2.0)))
    rods.sort(key=lambda r: r[0])
    cs = np.array([r[0] for r in rods]); l0s = np.array([r[1] for r in rods]); l1s = np.array([r[2] for r in rods])
    areas = np.array([r[3] for r in rods]); reach = np.array([r[4] for r in rods])
    # longitudinal columns of the raster
    n_long, n_cross = (nx, ny) if axis == "x" else (ny, nx)
    o_long, o_cross = (x0, y0) if axis == "x" else (y0, x0)
    cross_coords = o_cross + (np.arange(n_cross) + 0.5) * CELL
    for il in range(n_long):
        L = o_long + (il + 0.5) * CELL
        here = np.flatnonzero((l0s <= L) & (L <= l1s))
        if len(here) == 0:
            continue
        c = cs[here]; a = areas[here]; r = reach[here]
        gaps_lo = np.r_[np.inf, np.diff(c)] / 2.0; gaps_hi = np.r_[np.diff(c), np.inf] / 2.0
        w_lo = np.minimum(gaps_lo, r); w_hi = np.minimum(gaps_hi, r)
        # nearest rod for every cross cell
        idx = np.searchsorted(c, cross_coords)
        idx_lo = np.clip(idx - 1, 0, len(c) - 1); idx_hi = np.clip(idx, 0, len(c) - 1)
        d_lo = np.abs(cross_coords - c[idx_lo]); d_hi = np.abs(cross_coords - c[idx_hi])
        near = np.where(d_lo <= d_hi, idx_lo, idx_hi); dist = np.minimum(d_lo, d_hi)
        side_w = np.where(cross_coords < c[near], w_lo[near], w_hi[near])
        covered = dist <= side_w
        col = np.where(covered, a[near] / (w_lo[near] + w_hi[near]) * 1000.0 / 100.0, 0.0)  # mm²/mm -> cm²/m
        if axis == "x":
            dens[:, il] = col
        else:
            dens[il, :] = col
    return dens


def main():
    settings = Settings(min_internal_step=100)
    print(f"cover {COVER_MM} mm, reach r = 5(c + d/2); raster {CELL} mm")
    for scene, axis in (("A", "y"), ("B", "x"), ("C", "x")):
        base = json.load(open(f"bench/out/{scene}/base.json")); exp = json.load(open(f"bench/out/{scene}/exp30.json"))
        polys = extract_polygons(base["dxf"])
        rows = [{"idx": i, "points": [[float(x), float(y)] for x, y in p["points"]], "load": float(p["load"]), "color": int(p["color"])} for i, p in enumerate(polys)]
        pts = np.array([p for r in rows for p in r["points"]]); x0, y0 = pts.min(0) - 2 * CELL; x1, y1 = pts.max(0) + 2 * CELL
        nx, ny = int(np.ceil((x1 - x0) / CELL)), int(np.ceil((y1 - y0) / CELL))
        shapes = [Polygon(r["points"]) for r in rows]; areas = np.array([s.area / 1e6 for s in shapes])
        need = np.array([r["load"] for r in rows])
        print(f"\n{base['dxf'].split('/')[-1]}  ({len(rows)} polygons, raster {nx}x{ny})")
        for label, zones in (("production N=30", base["ns"]["30"]["zones"]), ("lattice pool K=30", exp["zones_prod"])):
            store = FakeStore(settings, rows)
            store.v2.create_bar_task("bars", scene_id="scene", overlay_id=0, smooth=False,
                                     config={"axis": axis, "anchor_factor": 40.0, "steel_density_kg_m3": 7850.0, "min_bar_gap_mm": None}, zones=zones)
            handle_bars_job(store, {"task_id": "bars"}, "bench"); bars = store.v2.get_bar_task("bars")["result"]["bars"]
            dens = density_raster(bars, axis, x0, y0, nx, ny)
            # mean density per polygon over raster cell centres inside it
            fact_s = np.zeros(len(rows))
            for i, s in enumerate(shapes):
                bx0, by0, bx1, by1 = s.bounds
                ix = np.arange(max(0, int((bx0 - x0) / CELL)), min(nx, int((bx1 - x0) / CELL) + 1))
                iy = np.arange(max(0, int((by0 - y0) / CELL)), min(ny, int((by1 - y0) / CELL) + 1))
                gx, gy = np.meshgrid(x0 + (ix + 0.5) * CELL, y0 + (iy + 0.5) * CELL)
                inside = contains_xy(s, gx.ravel(), gy.ravel())
                fact_s[i] = dens[np.ix_(iy, ix)].ravel()[inside].mean() if inside.any() else 0.0
            cyl = reinforcement_rows(rows, bars, steel_density_kg_m3=7850.0, t_mm=600.0)
            fact_c = np.array([r["fact_load_sm2/m"] or 0.0 for r in cyl])
            kg = lambda v: float((v * 1e-4 * areas * 7850.0).sum())
            for name, fact in (("cylinder (current)", fact_c), ("smeared (proposed)", fact_s)):
                short = np.maximum(0.0, need - fact)
                print(f"  {label:18s} {name:19s}: shortfall {kg(short):6.1f} kg = {100 * kg(short) / kg(need):4.2f}% | polygons short {int((short > 0.05).sum()):4d}, >1 cm²/m {int((short > 1).sum()):4d}, >3 cm²/m {int((short > 3).sum()):4d} | worst {short.max():5.2f} | mean fact/need {float((fact / np.maximum(need, 1e-9)).mean()):.2f}")


if __name__ == "__main__":
    main()
