"""Evaluate zone sets through the exact worker handlers behind POST /v2/bars and POST /v2/verification.

For each scene: production task zones (bench/out/<S>/base.json) vs lattice-pool zones (bench/out/<S>/exp30.json).
"""
from __future__ import annotations

import json, sys
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from A101.read_dxf import extract_polygons
from rebar_service.config import Settings
from rebar_service.overlays import resolve_overlay
from rebar_service.v2.workers import handle_bars_job, handle_verification_job
from support.memory_v2_store import MemoryV2Store


class FakeStore:
    def __init__(self, settings, rows):
        self.settings, self.rows, self.events, self.v2 = settings, rows, [], MemoryV2Store()
    def resolved_scene_polygons(self, scene_id, *, variant="raw", overlay_id=0):
        return resolve_overlay(self.rows, self.events, overlay_id)


def run(scene, axis, label, zones, rows, settings):
    store = FakeStore(settings, rows)
    config = {"axis": axis, "anchor_factor": 40.0, "steel_density_kg_m3": 7850.0, "t": 600.0, "min_bar_gap_mm": None}
    store.v2.create_bar_task("bars", scene_id="scene", overlay_id=0, smooth=False, config=config, zones=zones)
    handle_bars_job(store, {"task_id": "bars"}, "bench")
    bars = store.v2.get_bar_task("bars")
    store.v2.create_verification_task("ver", scene_id="scene", overlay_id=0, smooth=False, config=config, zones=zones)
    handle_verification_job(store, {"task_id": "ver"}, "bench")
    ver = store.v2.get_verification_task("ver")
    assert bars["state"] == "success" and ver["state"] == "success", (bars.get("error"), ver.get("error"))
    res = ver["result"]
    area = {int(r["idx"]): Polygon(r["points"]).area / 1e6 for r in rows}
    need = np.array([r["need_load_sm2/m"] or 0.0 for r in res]); fact = np.array([r["fact_load_sm2/m"] or 0.0 for r in res])
    a = np.array([area[int(r["source_index"])] for r in res])
    short = np.maximum(0.0, need - fact)
    kg = lambda v: float((v * 1e-4 * a * 7850.0).sum())
    mm = bars["result"]["mass_metrics"]
    return {"label": label, "zones_in": len(zones) - 1, "bars": len(bars["result"]["bars"]),
            "add_with_anch_kg": round(mm["additional"]["with_anchorage_kg"], 1), "bg_with_anch_kg": round(mm["bg"]["with_anchorage_kg"], 1),
            "need_kg": round(kg(need)), "fact_kg_capped": round(kg(np.minimum(fact, need))), "shortfall_kg": round(kg(short), 1),
            "shortfall_pct": round(100 * kg(short) / kg(need), 2), "polys_short": int((short > 0).sum()),
            "polys_short_gt1": int((short > 1.0).sum()), "worst_cm2_m": round(float(short.max()), 2)}


def main():
    settings = Settings(min_internal_step=100)
    out = []
    for scene, axis in (("A", "y"), ("B", "x"), ("C", "x")):
        base = json.load(open(f"bench/out/{scene}/base.json")); exp = json.load(open(f"bench/out/{scene}/exp30.json"))
        polys = extract_polygons(base["dxf"])
        rows = [{"idx": i, "points": [[float(x), float(y)] for x, y in p["points"]], "load": float(p["load"]), "color": int(p["color"])} for i, p in enumerate(polys)]
        name = base["dxf"].split("/")[-1]
        for label, zones in (("production N=30", base["ns"]["30"]["zones"]), ("lattice pool K=30", exp["zones_prod"])):
            r = run(scene, axis, label, zones, rows, settings); r["scene"] = name; out.append(r)
            print(f"{name[:30]:30s} {label:18s} zones {r['zones_in']:3d} bars {r['bars']:4d} | add+anch {r['add_with_anch_kg']:8.1f} kg | need {r['need_kg']:6d} kg, delivered(capped) {r['fact_kg_capped']:6d} kg, shortfall {r['shortfall_kg']:6.1f} kg = {r['shortfall_pct']:4.2f}% | polygons short {r['polys_short']:4d} (>1 cm²/m: {r['polys_short_gt1']:4d}), worst {r['worst_cm2_m']:5.2f} cm²/m")
    Path("bench/out/verify_like_endpoint.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
