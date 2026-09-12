"""Apply the validator-driven gap filler to stored solutions and report before/after."""
from __future__ import annotations

import json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests")); sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_all import RHO, T, COVER, OUT, FakeStore, draw  # noqa: E402
from A101.read_dxf import extract_polygons  # noqa: E402
from rebar_service.config import Settings  # noqa: E402
from rebar_service.v2.repair import fill_gaps  # noqa: E402
from rebar_service.v2.verification import reinforcement_rows  # noqa: E402
from rebar_service.v2.workers import handle_bars_job  # noqa: E402
import numpy as np  # noqa: E402
from shapely.geometry import Polygon  # noqa: E402


def stats(rows, bars):
    res = reinforcement_rows(rows, bars, steel_density_kg_m3=RHO, t_mm=T, cover_mm=COVER)
    areas = {int(r["idx"]): Polygon(r["points"]).area / 1e6 for r in rows}
    need = np.array([r["need_load_sm2/m"] or 0.0 for r in res]); fact = np.array([r["fact_load_sm2/m"] or 0.0 for r in res])
    a = np.array([areas[int(r["source_index"])] for r in res]); short = np.maximum(0.0, need - fact)
    per = {int(r["source_index"]): (need[i], fact[i], short[i]) for i, r in enumerate(res)}
    return {"short_kg": round(float((short * 1e-4 * a * RHO).sum()), 1), "polys_gt1": int((short > 1).sum()),
            "polys_gt05": int((short > 0.5).sum()), "worst": round(float(short.max()), 2)}, per


def main():
    table = []
    for scene, axis, name, dxf in (("A", "y", "Нижнее армирование вдоль ОСИ У", "A101_DXF/Нижнее армирование вдоль ОСИ У.dxf"),
                                   ("B", "x", "Верхняя по Х (1)", "A101_DXF/Верхняя по Х (1).dxf"),
                                   ("C", "x", "С5_Х низ", "A101_DXF/С5_Х низ.dxf")):
        polys = extract_polygons(dxf)
        rows = [{"idx": i, "points": [[float(x), float(y)] for x, y in p["points"]], "load": float(p["load"]), "color": int(p["color"])} for i, p in enumerate(polys)]
        sources = [("your solver, nearest", json.load(open(f"bench/out/{scene}/exp30.json"))["zones_prod"]),
                   ("production", json.load(open(f"bench/out/{scene}/base.json"))["ns"]["30"]["zones"])]
        for label, zones in sources:
            store = FakeStore(Settings(min_internal_step=100), rows)
            cfg = {"axis": axis, "anchor_factor": 40.0, "steel_density_kg_m3": RHO, "min_bar_gap_mm": None}
            store.v2.create_bar_task("b", scene_id="s", overlay_id=0, smooth=False, config=cfg, zones=zones)
            handle_bars_job(store, {"task_id": "b"}, "bench"); out = store.v2.get_bar_task("b")["result"]
            before, _ = stats(rows, out["bars"])
            t = time.perf_counter()
            fixed = fill_gaps(rows, out, axis=axis, anchor_factor=40.0, cover_mm=COVER, steel_density_kg_m3=RHO)
            dt = time.perf_counter() - t
            after, per = stats(rows, fixed["bars"])
            rep = fixed["repair"]
            row = {"scene": name, "solver": label, "mass_before": round(out["mass_metrics"]["additional"]["with_anchorage_kg"], 1),
                   "mass_after": round(fixed["mass_metrics"]["additional"]["with_anchorage_kg"], 1), "rods_added": rep["rods_added"],
                   "passes": rep["passes"], "repair_s": round(dt, 2), "before": before, "after": after}
            table.append(row)
            print(f"{name[:28]:28s} {label:22s} mass {row['mass_before']:8.1f} -> {row['mass_after']:8.1f} kg (+{row['mass_after']-row['mass_before']:.0f}, {rep['rods_added']} rods, {rep['passes']} passes, {dt:.1f} s) | short kg {before['short_kg']} -> {after['short_kg']} | polys>1 {before['polys_gt1']} -> {after['polys_gt1']} | worst {before['worst']} -> {after['worst']}")
            if label.startswith("your"):
                stem = f"{scene}_N30_user_solver_repaired"
                draw(rows, fixed, fixed["zones"], per, axis, f"{name} — your solver (nearest) + gap filling — {row['mass_after']:.0f} kg, {rep['rods_added']} rods added", stem)
    json.dump(table, open(OUT / "repair_table.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
