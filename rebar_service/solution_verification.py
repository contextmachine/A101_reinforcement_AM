from __future__ import annotations

from math import hypot, pi
from typing import Any, Mapping, Sequence

from shapely.geometry import Polygon
from shapely.strtree import STRtree


def reinforcement_area(diameter_mm: float, step_mm: float) -> float:
    diameter = float(diameter_mm)
    step = float(step_mm)
    if diameter <= 0 or step <= 0:
        raise ValueError("diameter and step must be positive")
    return 10.0 * pi * (diameter / 2.0) ** 2 / step


def compact_zone_rectangle(zone: Mapping[str, Any]):
    origin = zone.get("origin")
    direction = zone.get("direction")
    if not isinstance(origin, (list, tuple)) or len(origin) != 2:
        raise ValueError("zone.origin must contain x,y")
    if not isinstance(direction, (list, tuple)) or len(direction) != 2:
        raise ValueError("zone.direction must contain dx,dy")
    ox, oy = map(float, origin)
    dx, dy = map(float, direction)
    norm = hypot(dx, dy)
    if norm <= 0:
        raise ValueError("zone.direction must be non-zero")
    dx, dy = dx / norm, dy / norm
    length = float(zone.get("length", 0))
    step = float(zone.get("step", 0))
    right = int(zone.get("right", 0))
    left = int(zone.get("left", 0))
    if length <= 0 or step <= 0 or right < 0 or left < 0:
        raise ValueError("invalid compact zone dimensions")

    # Expand the transverse coverage by half a spacing on both sides.
    left_distance = left * step + step / 2.0
    right_distance = right * step + step / 2.0
    p0 = (ox - dx * left_distance, oy - dy * left_distance)
    p1 = (ox + dx * right_distance, oy + dy * right_distance)
    # bar direction = direction rotated counter-clockwise 90 degrees.
    bx, by = -dy, dx
    q1 = (p1[0] + bx * length, p1[1] + by * length)
    q0 = (p0[0] + bx * length, p0[1] + by * length)
    polygon = Polygon([p0, p1, q1, q0])
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon


def _polygon_geometry(row: Mapping[str, Any]):
    geometry = row.get("geometry")
    if geometry is not None and hasattr(geometry, "intersection"):
        return geometry
    points = row.get("points")
    if not points:
        raise ValueError("source polygon has no geometry/points")
    geometry = Polygon(points)
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    return geometry


def verify_compact_zones(
    polygons: Sequence[Mapping[str, Any]],
    zones: Sequence[Mapping[str, Any]],
    *,
    back_grid: Sequence[float] | None = None,
) -> list[float | None]:
    """Return reinforcement coverage percentages in stable source-polygon order."""
    zone_rows: list[tuple[Any, float]] = []
    for raw in zones:
        zone = dict(raw)
        geom = compact_zone_rectangle(zone)
        area = reinforcement_area(float(zone["d"]), float(zone["step"]))
        zone_rows.append((geom, area))
    zone_geoms = [row[0] for row in zone_rows]
    tree = STRtree(zone_geoms) if zone_geoms else None
    background = 0.0
    if back_grid is not None:
        if len(back_grid) != 2:
            raise ValueError("back_grid must be [diameter, step]")
        background = reinforcement_area(float(back_grid[0]), float(back_grid[1]))

    output: list[float | None] = []
    for raw in polygons:
        row = dict(raw)
        state = str(row.get("overlay_state", "active"))
        if state == "removed":
            output.append(None)
            continue
        if state == "background_only":
            output.append(100.0)
            continue
        polygon = _polygon_geometry(row)
        required = float(row.get("load", 0))
        if polygon.is_empty or polygon.area <= 0:
            raise ValueError("source polygon has zero area")
        if required <= 0:
            output.append(100.0)
            continue
        contribution = background * float(polygon.area)
        if tree is not None:
            for index in map(int, tree.query(polygon, predicate="intersects")):
                intersection_area = float(polygon.intersection(zone_geoms[index]).area)
                if intersection_area > 0:
                    contribution += intersection_area * float(zone_rows[index][1])
        percent = 100.0 * contribution / (float(polygon.area) * required)
        output.append(max(0.0, float(percent)))
    return output
