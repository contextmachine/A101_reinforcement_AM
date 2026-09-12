from __future__ import annotations

from math import hypot, isfinite, pi
from typing import Any, Mapping, Sequence

from shapely.geometry import LineString, Polygon

from .solution_verification import compact_zone_rectangle


def _layout_rebars(**kwargs):
    from A101.rebar_field_layout import layout_rebars
    return layout_rebars(**kwargs)


def _geometry(row: Mapping[str, Any]):
    value = row.get("geometry")
    if value is not None and hasattr(value, "intersection"):
        return value
    points = row.get("points")
    if not points:
        raise ValueError("source polygon has no geometry/points")
    geom = Polygon(points)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def _zone_parts(zones: Sequence[Mapping[str, Any]]) -> tuple[tuple[float, float], list[dict[str, Any]], list[dict[str, Any]]]:
    backgrounds = [dict(row) for row in zones if str(row.get("kind")) == "bg"]
    if len(backgrounds) != 1:
        raise ValueError("v2 zones must contain exactly one bg zone")
    bg_arm = dict(backgrounds[0].get("arm") or {})
    background = (float(bg_arm["d"]), float(bg_arm["step"]))
    if background[0] <= 0 or background[1] <= 0:
        raise ValueError("background arm must be positive")

    additional = [dict(row) for row in zones if str(row.get("kind")) == "additional"]
    boxes: list[dict[str, Any]] = []
    for row in additional:
        arm = dict(row.get("arm") or {})
        diameter, step = float(arm["d"]), float(arm["step"])
        if diameter <= 0 or step <= 0:
            raise ValueError("zone arm must be positive")
        geometry = compact_zone_rectangle({**row, "step": step})
        boxes.append({
            "id": int(row["id"]),
            "source_index": int(row["id"]),
            "geometry": geometry,
            "bounds": tuple(map(float, geometry.bounds)),
            "diameter": diameter,
            "step": step,
            # Keep these as metadata only. They never modify geometry here.
            "start_anchorage": float(row.get("start_anchorage", 0.0) or 0.0),
            "end_anchorage": float(row.get("end_anchorage", 0.0) or 0.0),
        })
    return background, additional, boxes


def layout_v2_zones(
    polygons: Sequence[Any],
    zones: Sequence[Mapping[str, Any]],
    *,
    axis: str,
    min_step: float,
) -> dict[str, Any]:
    """Run the allocator using visible compact-zone geometry only."""
    background, _additional, boxes = _zone_parts(zones)
    return dict(_layout_rebars(
        polygons=list(polygons), boxes=boxes, background=background,
        axis=str(axis), min_step=float(min_step),
    ) or {})


def _bar_length_mm(row: Mapping[str, Any]) -> float:
    return hypot(float(row["x1"]) - float(row["x0"]), float(row["y1"]) - float(row["y0"]))


def _mass_for_length(length_mm: float, diameter_mm: float, density: float) -> float:
    area_m2 = pi * (float(diameter_mm) / 1000.0) ** 2 / 4.0
    return float(density) * area_m2 * (float(length_mm) / 1000.0)


def v2_mass_metrics(
    bars: Sequence[Mapping[str, Any]],
    zones: Sequence[Mapping[str, Any]],
    *,
    steel_density_kg_m3: float,
    anchor_factor: float = 40.0,
) -> dict[str, Any]:
    """Mass visible bars and hidden anchorage without changing public geometry.

    Each clipped background segment gets ``anchor_factor * diameter`` at both
    ends. Additional segments retain their explicit zone anchorage lengths.
    """
    density = float(steel_density_kg_m3)
    factor = float(anchor_factor)
    if not isfinite(factor) or factor < 0:
        raise ValueError("anchor_factor must be finite and non-negative")
    if density <= 0:
        raise ValueError("steel_density_kg_m3 must be positive")
    _background, additional, _boxes = _zone_parts(zones)

    bg_visible = 0.0
    bg_with_anchor = 0.0
    add_visible = 0.0
    add_with_anchor = 0.0
    for raw in bars:
        row = dict(raw)
        diameter = float(row.get("diameter") or 0.0)
        if diameter <= 0:
            raise ValueError("bar diameter must be positive")
        length = _bar_length_mm(row)
        if bool(row.get("background")):
            bg_visible += _mass_for_length(length, diameter, density)
            bg_with_anchor += _mass_for_length(length + 2.0 * factor * diameter, diameter, density)
            continue
        index = int(row.get("input_zone_index", -1))
        if index < 0 or index >= len(additional):
            raise ValueError(f"bar references unknown additional zone index {index}")
        zone = additional[index]
        start = float(zone.get("start_anchorage", 0.0) or 0.0)
        end = float(zone.get("end_anchorage", 0.0) or 0.0)
        add_visible += _mass_for_length(length, diameter, density)
        add_with_anchor += _mass_for_length(length + start + end, diameter, density)

    # Unclipped diagnostics come from the compact source itself and therefore do
    # not recreate geometry outside the API. They are mass-only diagnostics.
    add_unclipped = 0.0
    add_anchor_unclipped = 0.0
    for zone in additional:
        arm = dict(zone.get("arm") or {})
        diameter = float(arm["d"])
        count = int(zone.get("left", 0)) + int(zone.get("right", 0)) + 1
        length = float(zone.get("length", 0.0))
        start = float(zone.get("start_anchorage", 0.0) or 0.0)
        end = float(zone.get("end_anchorage", 0.0) or 0.0)
        add_unclipped += count * _mass_for_length(length, diameter, density)
        add_anchor_unclipped += count * _mass_for_length(length + start + end, diameter, density)

    additional_metrics = {
        "with_anchorage_kg": float(add_with_anchor),
        "without_anchorage_kg": float(add_visible),
        "with_anchorage_unclipped_kg": float(add_anchor_unclipped),
        "without_anchorage_unclipped_kg": float(add_unclipped),
    }
    # Background diagnostics use the same clipped segments; only the hidden
    # anchorage differs. Do not extend their visible endpoints to add this mass.
    background_metrics = {
        "with_anchorage_kg": float(bg_with_anchor),
        "without_anchorage_kg": float(bg_visible),
        "with_anchorage_unclipped_kg": float(bg_with_anchor),
        "without_anchorage_unclipped_kg": float(bg_visible),
    }
    return {
        "additional": additional_metrics,
        "bg": background_metrics,
        "total_with_anchorage_kg": float(bg_with_anchor + add_with_anchor),
        "total_without_anchorage_kg": float(bg_visible + add_visible),
        "background_kg": float(bg_with_anchor),
    }


def public_v2_bars(
    layout: Mapping[str, Any], zones: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Return only visible clipped segments in the documented compact response."""
    _background, additional, _boxes = _zone_parts(zones)
    output: list[dict[str, Any]] = []
    for raw in layout.get("bars", []) or []:
        row = dict(raw)
        if bool(row.get("background")):
            zone_id = -1
        else:
            index = int(row.get("input_zone_index", -1))
            if index < 0 or index >= len(additional):
                raise ValueError(f"bar references unknown additional zone index {index}")
            zone_id = int(additional[index]["id"])
        output.append({
            "zone_id": zone_id,
            "start": [float(row["x0"]), float(row["y0"])],
            "end": [float(row["x1"]), float(row["y1"])],
            "d": float(row["diameter"]),
        })
    return output


def build_v2_bar_result(
    polygons: Sequence[Any],
    zones: Sequence[Mapping[str, Any]],
    *,
    axis: str,
    min_step: float,
    steel_density_kg_m3: float,
    anchor_factor: float = 40.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    layout = layout_v2_zones(polygons, zones, axis=axis, min_step=min_step)
    if not layout.get("is_feasible"):
        raise RuntimeError(str(layout.get("status") or layout.get("errors") or "bar layout failed"))
    raw_bars = [dict(row) for row in layout.get("bars", []) or []]
    metrics = v2_mass_metrics(
        raw_bars, zones, steel_density_kg_m3=steel_density_kg_m3, anchor_factor=anchor_factor,
    )
    result = {
        "bar_layout": {"bars": public_v2_bars(layout, zones)},
        "mass_metrics": {
            "additional": dict(metrics["additional"]),
            "bg": dict(metrics["bg"]),
        },
        "mass_kg": float(metrics["total_with_anchorage_kg"]),
        "mass_bg_kg": float(metrics["background_kg"]),
    }
    return result, raw_bars


def reinforcement_by_polygon(
    polygons: Sequence[Mapping[str, Any]],
    bars: Sequence[Mapping[str, Any]],
    *,
    steel_density_kg_m3: float,
    thickness_mm: float,
) -> list[dict[str, Any]]:
    """Compute required/factual reinforcement using visible bar-axis intersections."""
    density = float(steel_density_kg_m3)
    thickness = float(thickness_mm)
    if density <= 0 or thickness <= 0:
        raise ValueError("steel density and thickness must be positive")

    parsed_bars: list[tuple[LineString, float]] = []
    for raw in bars:
        row = dict(raw)
        start, end = row.get("start"), row.get("end")
        if not start or not end:
            raise ValueError("verification bar must contain start/end")
        diameter = float(row.get("d") or 0.0)
        if diameter <= 0:
            raise ValueError("verification bar diameter must be positive")
        parsed_bars.append((LineString([tuple(map(float, start)), tuple(map(float, end))]), diameter))

    output: list[dict[str, Any]] = []
    for raw in polygons:
        row = dict(raw)
        geom = _geometry(row)
        area = float(geom.area)
        if area <= 0:
            raise ValueError("source polygon has zero area")
        steel_volume_mm3 = 0.0
        for line, diameter in parsed_bars:
            length = float(line.intersection(geom).length)
            if length > 0:
                steel_volume_mm3 += length * pi * diameter**2 / 4.0
        steel_length_equivalent_mm = steel_volume_mm3 / area
        fact_sm2_m = 10.0 * steel_length_equivalent_mm
        need_sm2_m = float(row.get("load", 0.0) or 0.0)
        output.append({
            "source_index": int(row.get("source_index", len(output))),
            "overlay_state": str(row.get("overlay_state", "active")),
            "need_load_sm2/m": need_sm2_m,
            "fact_load_sm2/m": float(fact_sm2_m),
            "need_load_kg/m3": float((need_sm2_m * 0.1 / thickness) * density),
            "fact_load_kg/m3": float((steel_length_equivalent_mm / thickness) * density),
        })
    return output
