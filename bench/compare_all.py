"""Three solvers, one set of rules: production (exhaustive pool), production (lattice pool), user's
canon-pool + CP-SAT. Every solution goes through the /v2/bars and /v2/verification handlers
(smeared validator, cover 30 mm, t 600). Writes a table, JSON, and per-solution PNG + DXF drawings.
"""
from __future__ import annotations

import json, os, sys
from pathlib import Path

import ezdxf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection, PolyCollection
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_like_endpoint import FakeStore  # noqa: E402
from A101.read_dxf import extract_polygons  # noqa: E402
from rebar_service.config import Settings  # noqa: E402
from rebar_service.v2.bars import zone_to_box  # noqa: E402
from rebar_service.v2.workers import handle_bars_job, handle_verification_job  # noqa: E402

RHO, T, COVER = 7850.0, 600.0, 30.0
OUT = Path("bench/out/compare"); OUT.mkdir(parents=True, exist_ok=True)


def evaluate(rows, zones, axis, fill_gaps=True):
    store = FakeStore(Settings(min_internal_step=100), rows)
    cfg = {"axis": axis, "anchor_factor": 40.0, "steel_density_kg_m3": RHO, "t": T, "cover_mm": COVER, "min_bar_gap_mm": None,
           "fill_gaps": fill_gaps}
    store.v2.create_bar_task("b", scene_id="s", overlay_id=0, smooth=False, config=cfg, zones=zones)
    handle_bars_job(store, {"task_id": "b"}, "bench")
    bars = store.v2.get_bar_task("b")
    store.v2.create_verification_task("v", scene_id="s", overlay_id=0, smooth=False, config=cfg, zones=zones)
    handle_verification_job(store, {"task_id": "v"}, "bench")
    ver = store.v2.get_verification_task("v")
    if bars["state"] != "success" or ver["state"] != "success":
        return None, None, {"error": bars.get("error") or ver.get("error")}
    res = ver["result"]
    areas = {int(r["idx"]): Polygon(r["points"]).area / 1e6 for r in rows}
    need = np.array([r["need_load_sm2/m"] or 0.0 for r in res]); fact = np.array([r["fact_load_sm2/m"] or 0.0 for r in res])
    a = np.array([areas[int(r["source_index"])] for r in res]); short = np.maximum(0.0, need - fact)
    kg = lambda v: float((v * 1e-4 * a * RHO).sum())
    mm = bars["result"]["mass_metrics"]
    rep = bars["result"].get("repair") or {}
    metrics = {"add_with_anch_kg": round(mm["additional"]["with_anchorage_kg"], 1), "bg_with_anch_kg": round(mm["bg"]["with_anchorage_kg"], 1),
               "bars": len(bars["result"]["bars"]), "zones_out": sum(1 for z in bars["result"]["zones"] if z.get("kind") == "additional"),
               "rods_added": rep.get("rods_added", 0), "need_kg": round(kg(need)), "short_kg": round(kg(short), 1),
               "short_pct": round(100 * kg(short) / kg(need), 2), "polys_short": int((short > 0.05).sum()),
               "polys_short_gt1": int((short > 1).sum()), "worst_cm2_m": round(float(short.max()), 2)}
    return bars["result"], {int(r["source_index"]): (need[i], fact[i], short[i]) for i, r in enumerate(res)}, metrics


def draw(rows, bars_result, zones, per_poly, axis, title, stem):
    polys = [np.array(r["points"]) for r in rows]
    loads = np.array([r["load"] for r in rows])
    fig, ax = plt.subplots(figsize=(18, 18 * (max(p[:, 1].max() for p in polys) - min(p[:, 1].min() for p in polys)) / (max(p[:, 0].max() for p in polys) - min(p[:, 0].min() for p in polys))))
    pc = PolyCollection(polys, array=loads, cmap="YlOrBr", edgecolors="none", alpha=0.55); ax.add_collection(pc)
    short_polys = [polys[i] for i, r in enumerate(rows) if per_poly.get(int(r["idx"]), (0, 0, 0))[2] > 1.0]
    if short_polys:
        ax.add_collection(PolyCollection(short_polys, facecolors="none", edgecolors="red", linewidths=1.2, hatch="////"))
    bg = [b for b in bars_result["bars"] if b["zone_id"] == 0]; add = [b for b in bars_result["bars"] if b["zone_id"] != 0]
    ax.add_collection(LineCollection([[b["start"], b["end"]] for b in bg], colors="0.55", linewidths=0.4))
    ds = sorted({b["d"] for b in add}); cmap = plt.get_cmap("tab10")
    for k, d in enumerate(ds):
        segs = [[b["start"], b["end"]] for b in add if b["d"] == d]
        ax.add_collection(LineCollection(segs, colors=[cmap(k % 10)], linewidths=1.4, label=f"ø{d:g} ({len(segs)} bars)"))
    for z in zones:
        if z.get("kind") == "additional":
            x0, y0, x1, y1 = zone_to_box(z, axis=axis)["bounds"]
            ax.add_patch(plt.Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, edgecolor="blue", linewidth=0.8, linestyle="--"))
    ax.autoscale(); ax.set_aspect("equal"); ax.set_title(title, fontsize=11)
    ax.legend(loc="upper right", fontsize=8, title="additional bars; grey = bg; red hatch = short > 1 cm²/m; blue dashed = zones")
    fig.colorbar(pc, ax=ax, fraction=0.03, pad=0.01, label="need, cm²/m")
    fig.tight_layout(); fig.savefig(OUT / f"{stem}.png", dpi=110); plt.close(fig)
    doc = ezdxf.new("R2010"); msp = doc.modelspace()
    for name, color in (("FIELD", 8), ("BG_BARS", 9), ("ADD_BARS", 1), ("ZONES", 5), ("SHORT", 1)):
        doc.layers.add(name, color=color)
    for r in rows:
        msp.add_lwpolyline([tuple(p) for p in r["points"]], close=True, dxfattribs={"layer": "FIELD"})
        msp.add_text(f"{r['load']:g}", dxfattribs={"layer": "FIELD", "height": 60}).set_placement(tuple(np.mean(r["points"], axis=0)))
    for b in bars_result["bars"]:
        layer = "BG_BARS" if b["zone_id"] == 0 else "ADD_BARS"
        msp.add_line(tuple(b["start"]), tuple(b["end"]), dxfattribs={"layer": layer, "lineweight": int(b["d"]) * 5})
    for z in zones:
        if z.get("kind") == "additional":
            x0, y0, x1, y1 = zone_to_box(z, axis=axis)["bounds"]
            msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": "ZONES"})
            msp.add_text(f"ø{z['arm']['d']:g}@{z['arm']['step']:g} x{z.get('left', 0) + z.get('right', 0) + 1}", dxfattribs={"layer": "ZONES", "height": 80}).set_placement((x0, y1))
    for r in rows:
        n, f, s = per_poly.get(int(r["idx"]), (0, 0, 0))
        if s > 1.0:
            msp.add_lwpolyline([tuple(p) for p in r["points"]], close=True, dxfattribs={"layer": "SHORT"})
            msp.add_text(f"need {n:g} fact {f:.1f}", dxfattribs={"layer": "SHORT", "height": 50}).set_placement(tuple(np.mean(r["points"], axis=0)))
    doc.saveas(OUT / f"{stem}.dxf")


def prod_time(d, n):
    return round(d["stages"]["v2_prepare:None"] + sum(v for k, v in d["stages"].items() if k.endswith(f":{n}")), 1)


def main():
    table = []
    scenes = [("A", "y", "Нижнее армирование вдоль ОСИ У", [("production, exhaustive pool", "bench/out/A/base.json"), ("production, lattice cap 50k (L=2)", "bench/out/L/A_cap50000.json"), ("production, lattice cap 200k (L=1)", "bench/out/L/A_cap200000.json")]),
              ("B", "x", "Верхняя по Х (1)", [("production, exhaustive pool", "bench/out/B/base.json"), ("production, lattice cap 50k (L=3)", "bench/out/L/B_cap50000.json")]),
              ("C", "x", "С5_Х низ", [("production, exhaustive pool", "bench/out/C/base.json"), ("production, lattice cap 50k (L=2)", "bench/out/L/C_cap50000.json")])]
    for scene, axis, name, prods in scenes:
        base = json.load(open(prods[0][1]))
        polys = extract_polygons(base["dxf"])
        rows = [{"idx": i, "points": [[float(x), float(y)] for x, y in p["points"]], "load": float(p["load"]), "color": int(p["color"])} for i, p in enumerate(polys)]
        solutions = []
        for label, path in prods:
            d = json.load(open(path))
            for n in d["ns"]:
                if n != "30":
                    continue
                solutions.append((label, int(n), prod_time(d, n), d["ns"][n]["zones"]))
        exp = json.load(open(f"bench/out/{scene}/exp30.json"))
        solutions.append(("user solver: canon pool + CP-SAT", exp["k"], exp["t_total"], exp["zones_prod"]))
        ceil_path = Path(f"bench/out/{scene}/exp30_ceil.json")
        if ceil_path.exists():
            expc = json.load(open(ceil_path))
            solutions.append(("user solver: ceil band policy", expc["k"], expc["t_total"], expc["zones_prod"]))
        for label, n, seconds, zones in solutions:
            _b0, _p0, plain = evaluate(rows, zones, axis, fill_gaps=False)
            bars_result, per_poly, metrics = evaluate(rows, zones, axis, fill_gaps=True)
            metrics["without_fill"] = plain
            stem = f"{scene}_N{n}_" + label.split(",")[0].split(":")[0].replace(" ", "_") + ("_ceil" if "ceil" in label else "")
            if "lattice" in label:
                stem += "_lattice_L" + label.split("(L=")[1].rstrip(")")
            if bars_result is None:
                table.append({"scene": name, "solver": label, "N": n, "time_s": seconds, **metrics}); continue
            draw(rows, bars_result, zones, per_poly, axis, f"{name} — {label} — N={n}: {metrics['add_with_anch_kg']:.0f} kg additional, short {metrics['short_kg']:.1f} kg", stem)
            table.append({"scene": name, "solver": label, "N": n, "time_s": seconds, **metrics, "png": str(OUT / f"{stem}.png"), "dxf": str(OUT / f"{stem}.dxf")})
    json.dump(table, open(OUT / "table.json", "w"), ensure_ascii=False, indent=1)
    print(f"{'scene':30s} {'solver':36s} {'N':>3} {'time s':>7} | {'no fill: kg':>11} {'zones':>5} {'short kg':>8} {'polys>1':>7} | {'fill: kg':>9} {'+rods':>5} {'zones':>5} {'short kg':>8} {'polys>1':>7} {'worst':>6}")
    for r in table:
        w = r.get("without_fill") or {}
        print(f"{r['scene'][:30]:30s} {r['solver'][:36]:36s} {r['N']:3d} {r['time_s']:7.1f} | {w.get('add_with_anch_kg', float('nan')):11.1f} {w.get('zones_out', 0):5d} {w.get('short_kg', float('nan')):8.1f} {w.get('polys_short_gt1', 0):7d} | {r.get('add_with_anch_kg', float('nan')):9.1f} {r.get('rods_added', 0):5d} {r.get('zones_out', 0):5d} {r.get('short_kg', float('nan')):8.1f} {r.get('polys_short_gt1', 0):7d} {r.get('worst_cm2_m', float('nan')):6.2f}")


if __name__ == "__main__":
    main()
