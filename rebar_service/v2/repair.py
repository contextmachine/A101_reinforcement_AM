"""Validator-driven gap filling after the bar layout (``fill_gaps``).

A laid-out zone can leave a strip wider than its step: bars pushed off background guides by the
clearance rule, zones whose tightened box ends inside a high-need element, elements that straddle
a weak part of the period.  Instead of over-provisioning every zone, the layout is checked with the
same smeared verification that ``/v2/verification`` reports, and for every polygon still short one
rod is inserted into the widest gap of parallel rods crossing it.  The pass repeats until no polygon
is short by more than ``tol_cm2_m`` (or no rod can be added), so the result is covering by the
validator's own measure.

The inserted rods are returned as extra one-bar ``additional`` zones (so the zone list stays a
faithful description of the bars, and a second layout reproduces them) and as extra ``Bar``
rows; ``mass_metrics.additional`` is increased by their mass.
"""

from __future__ import annotations

from math import fsum, hypot, pi
from typing import Any, Mapping, Sequence

from shapely.geometry import LineString, MultiLineString
from shapely.ops import unary_union

from .bars import _canonical_direction, _frame, physical_polygons, zone_to_box
from .verification import bar_reach_mm, polygon_geometry, reinforcement_rows, wire_overlay_state


def fill_gaps(
    resolved_rows: Sequence[Mapping[str, Any]],
    layout_out: Mapping[str, Any],
    *,
    axis: str,
    anchor_factor: float,
    cover_mm: float,
    steel_density_kg_m3: float,
    tol_cm2_m: float = 0.5,
    max_passes: int = 30,
    max_rods: int = 2000,
) -> dict[str, Any]:
    """Return a copy of ``layout_out`` (bars, zones, mass_metrics) with gap-filling rods added."""
    axis = "x" if str(axis).lower() == "x" else "y"
    cross, long = _frame(axis)
    direction = _canonical_direction(axis)
    density = float(steel_density_kg_m3)
    bars = [dict(b) for b in layout_out.get("bars", []) or []]
    zones = [dict(z) for z in layout_out.get("zones", []) or []]
    metrics = {g: dict(v) for g, v in (layout_out.get("mass_metrics") or {}).items()}
    field = unary_union(physical_polygons(resolved_rows))
    geometries = [
        polygon_geometry(r) if wire_overlay_state(r.get("overlay_state")) != "empty" else None for r in resolved_rows
    ]
    additional_boxes = [
        (zone_to_box(z, axis=axis)["bounds"], float(z["arm"]["d"]), float(z["arm"]["step"]), float(z["origin"][cross]))
        for z in zones if z.get("kind") == "additional"
    ]
    next_id = max([int(z["id"]) for z in zones] + [0]) + 1
    report: dict[str, Any] = {"passes": 0, "rods_added": 0, "added_kg": 0.0, "short_before": None, "short_after": None}
    for _pass in range(int(max_passes)):
        verification = reinforcement_rows(
            resolved_rows, bars, steel_density_kg_m3=density, t_mm=1000.0, cover_mm=cover_mm,
        )
        short = []
        for position, v in enumerate(verification):
            need, fact = v.get("need_load_sm2/m"), v.get("fact_load_sm2/m")
            if need is None or fact is None or need - fact <= tol_cm2_m:
                continue
            short.append((position, need - fact))
        if report["short_before"] is None:
            report["short_before"] = {"polygons": len(short), "worst_cm2_m": round(max((s for _, s in short), default=0.0), 2)}
        if not short:
            break
        report["passes"] += 1
        proposals = []
        for position, deficit in short:
            proposal = _propose_rod(geometries[position], bars, additional_boxes, axis=axis, cover_mm=cover_mm)
            if proposal is not None:
                proposals.append(proposal)
        new_rods = _merge_proposals(proposals, cross=cross, long=long)
        if not new_rods:
            break
        added = 0
        for c_pos, l_lo, l_hi, d in new_rods:
            if report["rods_added"] >= max_rods:
                break
            anchor = float(anchor_factor) * d
            segments = _clip_to_field(field, axis, c_pos, l_lo - anchor, l_hi + anchor, long)
            for s_lo, s_hi in segments:
                if s_hi - s_lo <= 1.0:
                    continue
                start = [0.0, 0.0]; end = [0.0, 0.0]
                start[cross] = end[cross] = c_pos
                start[long], end[long] = s_lo, s_hi
                length = s_hi - s_lo
                bars.append({"zone_id": next_id, "start": start, "end": end, "d": d,
                             "anchorage": {"start": anchor, "end": anchor}})
                origin = list(start) if _bar_sign(direction, long) > 0 else list(end)
                zones.append({"id": next_id, "kind": "additional", "arm": {"d": d, "step": 100.0}, "left": 0, "right": 0,
                              "length": float(length), "anchorage": {"start": anchor, "end": anchor},
                              "origin": [float(origin[0]), float(origin[1])],
                              "direction": [float(direction[0]), float(direction[1])]})
                unit = density * pi * (d / 2.0) ** 2 * 1e-9  # kg per mm
                add = metrics.setdefault("additional", {})
                add["without_anchorage_kg"] = add.get("without_anchorage_kg", 0.0) + unit * length
                add["with_anchorage_kg"] = add.get("with_anchorage_kg", 0.0) + unit * (length + 2 * anchor)
                add["without_anchorage_unclipped_kg"] = add.get("without_anchorage_unclipped_kg", 0.0) + unit * length
                add["with_anchorage_unclipped_kg"] = add.get("with_anchorage_unclipped_kg", 0.0) + unit * (length + 2 * anchor)
                report["added_kg"] += unit * (length + 2 * anchor)
                report["rods_added"] += 1
                next_id += 1
                added += 1
        if added == 0:
            break
    verification = reinforcement_rows(resolved_rows, bars, steel_density_kg_m3=density, t_mm=1000.0, cover_mm=cover_mm)
    residual = [v["need_load_sm2/m"] - v["fact_load_sm2/m"] for v in verification
                if v.get("need_load_sm2/m") is not None and v.get("fact_load_sm2/m") is not None]
    report["short_after"] = {"polygons": sum(1 for s in residual if s > tol_cm2_m), "worst_cm2_m": round(max(residual, default=0.0), 2)}
    report["added_kg"] = round(report["added_kg"], 1)
    out = dict(layout_out)
    out.update({"bars": bars, "zones": zones, "mass_metrics": metrics, "repair": report})
    return out


def _bar_sign(direction: Sequence[float], long: int) -> float:
    # the bar runs 90° counter-clockwise from ``direction``
    bar = (-direction[1], direction[0])
    return 1.0 if bar[long] >= 0 else -1.0


def _propose_rod(polygon, bars, additional_boxes, *, axis: str, cover_mm: float):
    """Rod (cross position, long lo, long hi, d) filling the widest gap over one short polygon."""
    if polygon is None or polygon.is_empty:
        return None
    cross, long = _frame(axis)
    minx, miny, maxx, maxy = polygon.bounds
    lo = (minx, miny); hi = (maxx, maxy)
    c_lo, c_hi = lo[cross], hi[cross]
    l_lo, l_hi = lo[long], hi[long]
    centre = polygon.representative_point()
    cpt = (centre.x, centre.y)
    # diameter: the additional zone under the polygon, else the largest additional rod nearby, else nothing to do
    d = None
    grid = None  # (origin, step) of the strongest additional zone under the polygon
    for (bx0, by0, bx1, by1), zd, zstep, zorigin in additional_boxes:
        if bx0 - 1.0 <= cpt[0] <= bx1 + 1.0 and by0 - 1.0 <= cpt[1] <= by1 + 1.0:
            if d is None or zd > d:
                d, grid = zd, (zorigin, zstep)
    # parallel rods crossing the polygon's longitudinal span, by cross position
    rods = []
    for b in bars:
        s, e = b["start"], b["end"]
        b_lo, b_hi = min(s[long], e[long]), max(s[long], e[long])
        if b_hi < l_lo or b_lo > l_hi:
            continue
        pos = 0.5 * (s[cross] + e[cross])
        reach = bar_reach_mm(float(b["d"]), cover_mm)
        if pos < c_lo - reach or pos > c_hi + reach:
            continue
        rods.append((pos, float(b["d"]), int(b.get("zone_id", 0))))
    if d is None:
        add_ds = [rd for _, rd, zid in rods if zid != 0]
        if not add_ds:
            return None
        d = max(add_ds)
    rods.sort()
    # candidate gaps are measured between rods at least as strong as the zone: a thin background
    # bar inside a ø25 zone does not fill the zone's gap
    positions = [p for p, rd, _ in rods if rd >= d - 1e-6]
    all_positions = [(p, rd) for p, rd, _ in rods]
    gaps = []
    if positions:
        if positions[0] > c_lo:
            gaps.append((positions[0] - c_lo, c_lo, positions[0]))
        if positions[-1] < c_hi:
            gaps.append((c_hi - positions[-1], positions[-1], c_hi))
        for a, b in zip(positions[:-1], positions[1:]):
            if b > c_lo and a < c_hi:
                gaps.append((b - a, a, b))
    else:
        gaps.append((c_hi - c_lo, c_lo, c_hi))
    width, a, b = max(gaps, key=lambda g: (round(g[0], 6), -abs(0.5 * (g[1] + g[2]) - 0.5 * (c_lo + c_hi))))
    if width <= d + 1.0:
        return None
    position = 0.5 * (a + b)
    if grid is not None:
        # a free grid position of the zone inside the gap keeps the rod a member of that zone
        origin, step = grid
        k = round((position - origin) / step)
        candidate = origin + k * step
        if a + d <= candidate <= b - d:
            position = candidate
    # keep the clearance to every rod already there (a thinner bar on the same line included)
    for p, rd in all_positions:
        clearance = (d + rd) / 2.0
        if abs(position - p) < clearance - 1e-6:
            up, down = p + clearance, p - clearance
            position = up if (b - up) >= (down - a) else down
    if position <= a or position >= b:
        return None
    return (position, l_lo, l_hi, d)


def _merge_proposals(proposals, *, cross: int, long: int):
    """Merge proposals that share a cross position (within a diameter) and overlap/touch along the bars."""
    merged = []
    for c_pos, l_lo, l_hi, d in sorted(proposals):
        for m in merged:
            if abs(m[0] - c_pos) <= max(d, m[3]) and l_lo <= m[2] + 2 * 40 * d and l_hi >= m[1] - 2 * 40 * d:
                m[1], m[2], m[3] = min(m[1], l_lo), max(m[2], l_hi), max(m[3], d)
                break
        else:
            merged.append([c_pos, l_lo, l_hi, d])
    return [tuple(m) for m in merged]


def _clip_to_field(field, axis: str, c_pos: float, l_lo: float, l_hi: float, long: int):
    """Visible segments (along the bar) of a rod at cross position ``c_pos`` inside the field."""
    if axis == "x":
        line = LineString([(l_lo, c_pos), (l_hi, c_pos)])
    else:
        line = LineString([(c_pos, l_lo), (c_pos, l_hi)])
    inside = line.intersection(field)
    parts = []
    if inside.is_empty:
        return parts
    geoms = inside.geoms if isinstance(inside, MultiLineString) else [inside]
    for g in geoms:
        if g.geom_type != "LineString" or g.is_empty:
            continue
        coords = list(g.coords)
        a, b = coords[0][long], coords[-1][long]
        parts.append((min(a, b), max(a, b)))
    return parts
