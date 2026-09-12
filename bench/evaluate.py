"""One evaluator for every method: production bar layout (mass with anchorage) + production verification."""
from __future__ import annotations

from typing import Any, Sequence

from rebar_service.v2.bars import layout_zones, physical_polygons
from rebar_service.v2.verification import reinforcement_rows


def evaluate(rows: Sequence[dict], zones: Sequence[dict] | None = None, *, axis: str, anchor_factor: float = 40.0,
             density: float = 7850.0, min_step: float = 100.0, t_mm: float = 600.0, tol: float = 0.01,
             bars: Sequence[dict] | None = None, mass_metrics: dict | None = None) -> dict[str, Any]:
    """Lay out ``zones`` once (as the pipeline's bars stage does) or verify given ``bars`` + ``mass_metrics``."""
    if bars is None:
        polygons = physical_polygons(rows)
        out = layout_zones(polygons, list(zones or []), axis=axis, anchor_factor=anchor_factor,
                           steel_density_kg_m3=density, min_step=min_step)
        if not out.get("is_feasible"):
            return {"feasible": False, "status": out.get("status"), "warnings": out.get("warnings"), "errors": out.get("errors")}
        bars, mass_metrics = out["bars"], out["mass_metrics"]
        zones_out = len(out["zones"])
    else:
        zones_out = None
    verification = reinforcement_rows(rows, bars, steel_density_kg_m3=density, t_mm=t_mm)
    deficits = []
    deficit_kg = 0.0        # steel missing where fact < need, summed over polygons (kg equivalent)
    need_kg_total = 0.0     # total need over active polygons (same units), for scale
    from shapely.geometry import Polygon
    areas = {int(r.get("idx", i)): Polygon(r["points"]).area / 1e6 for i, r in enumerate(rows)}  # m²
    for v in verification:
        need, fact = v.get("need_load_sm2/m"), v.get("fact_load_sm2/m")
        if need is None or fact is None:
            continue
        area = areas.get(int(v.get("source_index")), 0.0)
        need_kg_total += need * 1e-4 * area * density
        if fact < need * (1.0 - tol):
            deficits.append((v.get("source_index"), need, fact))
            deficit_kg += (need - fact) * 1e-4 * area * density
    add = mass_metrics["additional"]
    return {
        "feasible": True,
        "additional_with_anchorage_kg": round(add["with_anchorage_kg"], 1),
        "additional_without_anchorage_kg": round(add["without_anchorage_kg"], 1),
        "bg_with_anchorage_kg": round(mass_metrics["bg"]["with_anchorage_kg"], 1),
        "bars": len(bars), "zones_out": zones_out,
        "deficit_polygons": len(deficits),
        "deficit_kg": round(deficit_kg, 1), "need_kg_total": round(need_kg_total, 1),
        "max_deficit_cm2_m": round(max((n - f for _, n, f in deficits), default=0.0), 2),
        "deficit_examples": deficits[:5],
    }
