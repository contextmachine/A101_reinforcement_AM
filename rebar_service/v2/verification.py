"""Per-polygon reinforcement verification for ``/v2/verification`` (design §6).

Pure functions on plain dicts.  Inputs:

* ``resolved_rows`` — rows produced by ``PostgresStore.resolved_scene_polygons``:
  ``{points, load, color?, overlay_state: active|background_only|removed, source_index}``.
* ``bars`` — ``Bar`` dicts ``{zone_id, start:[x,y], end:[x,y], d, anchorage:{start,end}}``.
  ``start``/``end`` are the visible (clipped) bar axis without anchorage; ``anchorage`` is
  ignored here because verification only counts steel that physically lies inside a polygon.

Output rows use the v2 wire overlay vocabulary ``active | real | empty``.  The translation is
kept local on purpose: this module must import without the rest of the v2 package.

Formulas (``t`` and every length in mm, ``ρ`` = ``steel_density_kg_m3``):

* ``need_load_sm2/m`` = polygon ``load`` for ``active``, ``0`` for ``real``, ``None`` for ``empty``.
* ``fact_load_sm2/m`` = ``10 · Σ_bars π(d/2)² · len(bar axis ∩ polygon) / area(polygon)``.
* ``need_load_kg/m3`` = ``need · ρ / (10 · t)``.
* ``fact_load_kg/m3`` = ``Σ_bars π(d/2)² · len · ρ / (area · t)``
  (identical to ``fact · ρ / (10 · t)``).
"""

from __future__ import annotations

from math import pi
from typing import Any, Mapping, Sequence

from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

# Stored/internal overlay state -> wire vocabulary.  Wire values pass through unchanged so the
# function accepts rows that were already serialised for the API.
_WIRE_OVERLAY_STATE: dict[str, str] = {
    "active": "active",
    "background_only": "real",
    "removed": "empty",
    "real": "real",
    "empty": "empty",
}

ROW_KEYS = (
    "source_index",
    "overlay_state",
    "need_load_sm2/m",
    "fact_load_sm2/m",
    "need_load_kg/m3",
    "fact_load_kg/m3",
)


def wire_overlay_state(state: Any) -> str:
    """Translate a stored overlay state into the v2 wire vocabulary ``active|real|empty``."""
    key = str(state if state is not None else "active").strip().lower() or "active"
    try:
        return _WIRE_OVERLAY_STATE[key]
    except KeyError:
        raise ValueError(f"unknown overlay_state: {state!r}") from None


def bar_axis(bar: Mapping[str, Any]) -> LineString:
    """Return the visible bar axis (no anchorage) as a shapely ``LineString``."""
    start = _point(bar.get("start"), "start")
    end = _point(bar.get("end"), "end")
    return LineString([start, end])


def bar_section_area_mm2(bar: Mapping[str, Any]) -> float:
    """Cross-section area ``π(d/2)²`` of one bar in mm²."""
    diameter = float(bar.get("d", 0.0) or 0.0)
    if diameter <= 0:
        raise ValueError("bar.d must be positive")
    return pi * (diameter / 2.0) ** 2


def polygon_geometry(row: Mapping[str, Any]) -> BaseGeometry:
    """Build (and repair with ``buffer(0)``) the shapely polygon of one resolved row."""
    geometry = row.get("geometry")
    if isinstance(geometry, BaseGeometry):
        polygon = geometry
    else:
        points = row.get("points")
        if not points or len(points) < 3:
            raise ValueError("source polygon must have at least 3 points")
        polygon = Polygon([(float(x), float(y)) for x, y in points])
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon


def reinforcement_rows(
    resolved_rows: Sequence[Mapping[str, Any]],
    bars: Sequence[Mapping[str, Any]],
    *,
    steel_density_kg_m3: float,
    t_mm: float,
) -> list[dict[str, Any]]:
    """Return one verification row per source polygon, in input order (design §6).

    ``removed`` (wire ``empty``) polygons get ``None`` in all four load fields.  A polygon whose
    repaired geometry has no area cannot host a fact value and gets ``fact_* = None`` while its
    ``need_*`` values are still reported.
    """
    density = float(steel_density_kg_m3)
    thickness = float(t_mm)
    if density <= 0:
        raise ValueError("steel_density_kg_m3 must be positive")
    if thickness <= 0:
        raise ValueError("t must be positive")

    axes: list[LineString] = []
    sections: list[float] = []
    for bar in bars:
        axis = bar_axis(bar)
        if axis.is_empty or axis.length <= 0:
            continue
        axes.append(axis)
        sections.append(bar_section_area_mm2(bar))
    tree = STRtree(axes) if axes else None

    # Material polygons (active + real) and their geometries; a bar that runs exactly along an
    # edge shared by two material polygons belongs half to each of them.
    states: list[str] = [wire_overlay_state(raw.get("overlay_state")) for raw in resolved_rows]
    geometries: list[Any] = [
        polygon_geometry(raw) if state != "empty" else None for raw, state in zip(resolved_rows, states)
    ]
    material_indices = [i for i, geom in enumerate(geometries) if geom is not None and not geom.is_empty]
    material_tree = STRtree([geometries[i] for i in material_indices]) if material_indices else None

    def shared_boundary(position: int, polygon: Any):
        if material_tree is None:
            return None
        pieces = []
        for local in map(int, material_tree.query(polygon, predicate="intersects")):
            other = material_indices[local]
            if other == position:
                continue
            touch = polygon.boundary.intersection(geometries[other].boundary)
            if not touch.is_empty and touch.length > 0:
                pieces.append(touch)
        if not pieces:
            return None
        return unary_union(pieces)

    rows: list[dict[str, Any]] = []
    for position, raw in enumerate(resolved_rows):
        source_index = int(raw.get("source_index", position))
        state = states[position]
        if state == "empty":
            rows.append(_row(source_index, state, None, None, None, None))
            continue

        need = float(raw.get("load", 0.0) or 0.0) if state == "active" else 0.0
        need_kg = need * density / (10.0 * thickness)

        polygon = geometries[position]
        area = float(polygon.area) if not polygon.is_empty else 0.0
        if area <= 0:
            rows.append(_row(source_index, state, need, None, need_kg, None))
            continue

        steel_volume = 0.0  # mm³ of bar cylinders whose axis lies inside the polygon
        if tree is not None:
            shared = shared_boundary(position, polygon)
            for index in map(int, tree.query(polygon, predicate="intersects")):
                inside = float(axes[index].intersection(polygon).length)
                if shared is not None and inside > 0:
                    inside -= 0.5 * float(axes[index].intersection(shared).length)
                if inside > 0:
                    steel_volume += sections[index] * inside
        fact = 10.0 * steel_volume / area
        fact_kg = steel_volume * density / (area * thickness)
        rows.append(_row(source_index, state, need, fact, need_kg, fact_kg))
    return rows


def _row(
    source_index: int,
    state: str,
    need: float | None,
    fact: float | None,
    need_kg: float | None,
    fact_kg: float | None,
) -> dict[str, Any]:
    return dict(zip(ROW_KEYS, (source_index, state, need, fact, need_kg, fact_kg)))


def _point(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"bar.{name} must contain x,y")
    return float(value[0]), float(value[1])
