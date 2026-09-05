from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np
from shapely.geometry import Polygon


def _points_from_row(row: Mapping[str, Any]) -> list[list[float]]:
    points = row.get("points")
    if points is None and row.get("geometry") is not None:
        geometry = row["geometry"]
        points = list(geometry.exterior.coords)[:-1]
    if points is None:
        raise ValueError("polygon row does not contain points/geometry")
    return [[float(point[0]), float(point[1])] for point in points]


def canonicalize_polygons(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return stable JSON-safe polygons without Shapely/runtime smoothing metadata."""

    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {
            "points": _points_from_row(row),
            "load": float(row["load"]),
        }
        if row.get("color") is not None:
            item["color"] = int(row["color"])
        out.append(item)
    return out


def geometry_polygons(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the algorithm's Shapely/Numpy polygon rows from canonical JSON."""

    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        points = np.asarray(row["points"], dtype=float)
        geometry = Polygon(points)
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty or geometry.area <= 0:
            raise ValueError(f"Некорректный полигон #{index}")
        item: dict[str, Any] = {"points": points, "geometry": geometry, "load": float(row["load"])}
        if row.get("color") is not None:
            item["color"] = int(row["color"])
        out.append(item)
    if not out:
        raise ValueError("Не переданы полигоны")
    return out


def build_polygon_variants(input_obj: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Parse source once and persist both raw and smooth canonical variants."""

    from A101.read_dxf import smooth_load

    from .source_polygons import source_polygons_from_input

    raw = canonicalize_polygons(source_polygons_from_input(input_obj))
    smooth = canonicalize_polygons(smooth_load(geometry_polygons(raw)))
    return {"raw": raw, "smooth": smooth}
