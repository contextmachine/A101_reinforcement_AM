"""Per-polygon reinforcement verification for ``/v2/verification`` (design §6).

Pure functions on plain dicts.  Inputs:

* ``resolved_rows`` — rows produced by ``PostgresStore.resolved_scene_polygons``:
  ``{points, load, color?, overlay_state: active|background_only|removed, source_index}``.
* ``bars`` — ``Bar`` dicts ``{zone_id, start:[x,y], end:[x,y], d, anchorage:{start,end}}``.
  ``start``/``end`` are the visible (clipped) bar axis without anchorage; ``anchorage`` is
  ignored here because verification only credits steel that physically lies in the field.

Output rows use the v2 wire overlay vocabulary ``active | real | empty``.  The translation is
kept local on purpose: this module must import without the rest of the v2 package.

Model (``t``, ``c`` and every length in mm, ``ρ`` = ``steel_density_kg_m3``):

Reinforcement is a *set of rods*, each with only an axis and a diameter ``d``; zones and their
nominal spacing play no role.  A rod acts on the slab through the strip of concrete around it
(its tributary band): at every point along the rod the band reaches half-way to the nearest
parallel rod on each side, but never farther than the crack-control reach
``r = 5 · (c + d/2)`` of EN 1992-1-1 §7.3.4 (``c`` = concrete cover to the bar face, so
``c + d/2`` is the depth of the rod axis).  The smeared reinforcement density of a point is

    density = 10 · π(d/2)² / (w_left + w_right)   [cm²/m]

of the nearest rod whose band covers the point (``w`` = the two half-widths of that band), and
``0`` where no band reaches.  A uniform mesh at spacing ``s ≤ 2r`` therefore reads exactly
``10·π(d/2)²/s`` everywhere, independently of how the finite elements are cut, and the strip
between two rods farther apart than ``2r`` is unreinforced.  The density is evaluated on a
raster of ``raster_mm`` cells over the field and averaged over the cells inside each polygon.

* ``need_load_sm2/m`` = polygon ``load`` for ``active``, ``0`` for ``real``, ``None`` for ``empty``.
* ``fact_load_sm2/m`` = mean smeared density over the polygon.
* ``need_load_kg/m3`` = ``need · ρ / (10 · t)``.
* ``fact_load_kg/m3`` = ``fact · ρ / (10 · t)``.
"""

from __future__ import annotations

from math import atan2, cos, hypot, pi, sin
from typing import Any, Mapping, Sequence

import numpy as np
from shapely import contains_xy
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

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

DEFAULT_COVER_MM = 30.0
DEFAULT_RASTER_MM = 20.0
REACH_FACTOR = 5.0  # EN 1992-1-1 §7.3.4: a bar controls concrete within 5·(c + d/2) of its axis


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


def bar_reach_mm(diameter: float, cover_mm: float) -> float:
    """Lateral reach ``5·(c + d/2)`` of one rod (EN 1992-1-1 §7.3.4)."""
    return REACH_FACTOR * (float(cover_mm) + float(diameter) / 2.0)


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


class SmearedDensity:
    """Smeared reinforcement density field of a set of parallel rods on a raster.

    The rod direction is taken from the rods themselves (the dominant direction); the raster is
    laid out in the frame ``(long, cross)`` of that direction so that the tributary bands are
    exact for every rod family the bar layout produces.
    """

    def __init__(
        self,
        bars: Sequence[Mapping[str, Any]],
        bounds: tuple[float, float, float, float],
        *,
        cover_mm: float,
        raster_mm: float,
    ) -> None:
        self.cell = float(raster_mm)
        rods = [
            (_point(b.get("start"), "start"), _point(b.get("end"), "end"), bar_section_area_mm2(b),
             bar_reach_mm(float(b.get("d", 0.0) or 0.0), cover_mm))
            for b in bars
        ]
        rods = [r for r in rods if hypot(r[1][0] - r[0][0], r[1][1] - r[0][1]) > 0.0]
        self.angle = _dominant_angle(rods)
        ca, sa = cos(self.angle), sin(self.angle)
        # frame: long = along the rods, cross = perpendicular
        self._to_frame = lambda x, y: (x * ca + y * sa, -x * sa + y * ca)
        x0, y0, x1, y1 = bounds
        corners = [self._to_frame(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        self.l0 = min(c[0] for c in corners) - self.cell
        self.c0 = min(c[1] for c in corners) - self.cell
        l1 = max(c[0] for c in corners) + self.cell
        c1 = max(c[1] for c in corners) + self.cell
        self.n_long = max(1, int(np.ceil((l1 - self.l0) / self.cell)))
        self.n_cross = max(1, int(np.ceil((c1 - self.c0) / self.cell)))
        self.field = np.zeros((self.n_long, self.n_cross), dtype=float)
        if rods:
            self._rasterise(rods)

    def _rasterise(self, rods: list[tuple[tuple[float, float], tuple[float, float], float, float]]) -> None:
        cross = np.empty(len(rods)); lo = np.empty(len(rods)); hi = np.empty(len(rods))
        area = np.empty(len(rods)); reach = np.empty(len(rods))
        for i, (start, end, a, r) in enumerate(rods):
            s = self._to_frame(*start); e = self._to_frame(*end)
            cross[i] = 0.5 * (s[1] + e[1])
            lo[i], hi[i] = min(s[0], e[0]), max(s[0], e[0])
            area[i], reach[i] = a, r
        cross_centres = self.c0 + (np.arange(self.n_cross) + 0.5) * self.cell
        long_centres = self.l0 + (np.arange(self.n_long) + 0.5) * self.cell
        # the set of rods present is constant between consecutive rod end points
        breaks = np.unique(np.concatenate([lo, hi]))
        for a, b in zip(breaks[:-1], breaks[1:]):
            cols = np.flatnonzero((long_centres >= a) & (long_centres < b))
            if len(cols) == 0:
                continue
            mid = 0.5 * (a + b)
            active = np.flatnonzero((lo <= mid) & (mid <= hi))
            if len(active) == 0:
                continue
            profile = _cross_profile(cross[active], area[active], reach[active], cross_centres)
            self.field[cols, :] = profile

    def mean_over(self, polygon: BaseGeometry) -> float | None:
        """Mean density over the raster cells inside ``polygon`` (``None`` if it has no area)."""
        if polygon.is_empty or polygon.area <= 0:
            return None
        minx, miny, maxx, maxy = polygon.bounds
        corners = [self._to_frame(x, y) for x, y in ((minx, miny), (maxx, miny), (minx, maxy), (maxx, maxy))]
        fl0 = min(c[0] for c in corners); fl1 = max(c[0] for c in corners)
        fc0 = min(c[1] for c in corners); fc1 = max(c[1] for c in corners)
        il = np.arange(max(0, int((fl0 - self.l0) / self.cell)), min(self.n_long, int((fl1 - self.l0) / self.cell) + 1))
        ic = np.arange(max(0, int((fc0 - self.c0) / self.cell)), min(self.n_cross, int((fc1 - self.c0) / self.cell) + 1))
        if len(il) == 0 or len(ic) == 0:
            return float(self._at(polygon.representative_point()))
        gl, gc = np.meshgrid(self.l0 + (il + 0.5) * self.cell, self.c0 + (ic + 0.5) * self.cell, indexing="ij")
        ca, sa = cos(self.angle), sin(self.angle)
        gx = gl * ca - gc * sa
        gy = gl * sa + gc * ca
        inside = contains_xy(polygon, gx.ravel(), gy.ravel())
        if not inside.any():
            return float(self._at(polygon.representative_point()))
        return float(self.field[np.ix_(il, ic)].ravel()[inside].mean())

    def _at(self, point: Any) -> float:
        l, c = self._to_frame(float(point.x), float(point.y))
        il = min(max(int((l - self.l0) / self.cell), 0), self.n_long - 1)
        ic = min(max(int((c - self.c0) / self.cell), 0), self.n_cross - 1)
        return float(self.field[il, ic])


def _dominant_angle(rods: Sequence[tuple[tuple[float, float], tuple[float, float], float, float]]) -> float:
    """Direction (radians, in ``[0, π)``) carried by the largest total rod length."""
    if not rods:
        return 0.0
    # cluster angles modulo π by summing unit doubles (axial data)
    sx = sy = 0.0
    for start, end, _, _ in rods:
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = hypot(dx, dy)
        theta = atan2(dy, dx)
        sx += length * cos(2.0 * theta)
        sy += length * sin(2.0 * theta)
    return 0.5 * atan2(sy, sx) % pi


def _cross_profile(cross: np.ndarray, area: np.ndarray, reach: np.ndarray, centres: np.ndarray) -> np.ndarray:
    """Density (cm²/m) along the cross axis for one set of simultaneously present rods."""
    order = np.argsort(cross)
    cross, area, reach = cross[order], area[order], reach[order]
    # merge rods sitting on the same line (stacked layers): areas add, reach is the largest
    keep = np.r_[True, np.diff(cross) > 1e-6]
    if not keep.all():
        groups = np.cumsum(keep) - 1
        cross = cross[keep]
        area = np.bincount(groups, weights=area)
        reach = np.maximum.reduceat(reach, np.flatnonzero(keep))
    half_lo = np.r_[np.inf, np.diff(cross)] / 2.0
    half_hi = np.r_[np.diff(cross), np.inf] / 2.0
    w_lo = np.minimum(half_lo, reach)
    w_hi = np.minimum(half_hi, reach)
    idx = np.searchsorted(cross, centres)
    i_lo = np.clip(idx - 1, 0, len(cross) - 1)
    i_hi = np.clip(idx, 0, len(cross) - 1)
    d_lo = np.abs(centres - cross[i_lo])
    d_hi = np.abs(centres - cross[i_hi])
    near = np.where(d_lo <= d_hi, i_lo, i_hi)
    dist = np.minimum(d_lo, d_hi)
    side = np.where(centres < cross[near], w_lo[near], w_hi[near])
    covered = dist <= side
    width = w_lo[near] + w_hi[near]
    return np.where(covered, 10.0 * area[near] / width, 0.0)


def reinforcement_rows(
    resolved_rows: Sequence[Mapping[str, Any]],
    bars: Sequence[Mapping[str, Any]],
    *,
    steel_density_kg_m3: float,
    t_mm: float,
    cover_mm: float = DEFAULT_COVER_MM,
    raster_mm: float = DEFAULT_RASTER_MM,
) -> list[dict[str, Any]]:
    """Return one verification row per source polygon, in input order (design §6).

    ``removed`` (wire ``empty``) polygons get ``None`` in all four load fields.  A polygon whose
    repaired geometry has no area cannot host a fact value and gets ``fact_* = None`` while its
    ``need_*`` values are still reported.
    """
    density = float(steel_density_kg_m3)
    thickness = float(t_mm)
    cover = float(cover_mm)
    if density <= 0:
        raise ValueError("steel_density_kg_m3 must be positive")
    if thickness <= 0:
        raise ValueError("t must be positive")
    if cover < 0:
        raise ValueError("cover_mm must be non-negative")
    if float(raster_mm) <= 0:
        raise ValueError("raster_mm must be positive")

    states: list[str] = [wire_overlay_state(raw.get("overlay_state")) for raw in resolved_rows]
    geometries: list[Any] = [
        polygon_geometry(raw) if state != "empty" else None for raw, state in zip(resolved_rows, states)
    ]
    material = [g for g in geometries if g is not None and not g.is_empty]
    smeared = None
    if material and bars:
        xs0 = min(g.bounds[0] for g in material); ys0 = min(g.bounds[1] for g in material)
        xs1 = max(g.bounds[2] for g in material); ys1 = max(g.bounds[3] for g in material)
        smeared = SmearedDensity(bars, (xs0, ys0, xs1, ys1), cover_mm=cover, raster_mm=float(raster_mm))

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
        if polygon.is_empty or float(polygon.area) <= 0:
            rows.append(_row(source_index, state, need, None, need_kg, None))
            continue
        fact = 0.0 if smeared is None else smeared.mean_over(polygon)
        if fact is None:
            rows.append(_row(source_index, state, need, None, need_kg, None))
            continue
        rows.append(_row(source_index, state, need, fact, need_kg, fact * density / (10.0 * thickness)))
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
