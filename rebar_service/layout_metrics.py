"""Mass diagnostics for one fixed physical layout (coordinates in mm, masses in kg).

The four metrics never run a second layout/solver. A track is identified by ID,
not just its transverse coordinate: separate rods must keep their multiplicity.
A hole may split one track into several physical segments; its uncut diagnostic
contains one full rod. Background is the same clipped background in all totals.
"""
from __future__ import annotations

from collections import defaultdict
from math import fsum, hypot, isclose, isfinite, pi
from typing import Any, Mapping, Sequence

MASS_METRICS_VERSION = 2
_MASS_KEYS = (
    "without_anchorage_kg", "with_anchorage_kg",
    "without_anchorage_unclipped_kg", "with_anchorage_unclipped_kg",
)
_EPS = 1e-6


class LayoutMetricError(ValueError):
    """Inconsistent geometry/provenance; do not persist a misleading mass."""


def _bar(raw: Any) -> tuple[float, float, float, float]:
    values = (raw[k] for k in ("x0", "y0", "x1", "y1")) if isinstance(raw, Mapping) else raw[:4]
    result = tuple(map(float, values))
    if len(result) != 4 or not all(map(isfinite, result)):
        raise LayoutMetricError("Стержень должен содержать четыре конечные координаты")
    return result


def _mass(bars: Sequence[Any], diameter: float, density: float) -> float:
    area = pi * (diameter / 1000.0) ** 2 / 4
    return density * area * fsum(hypot(b[2] - b[0], b[3] - b[1]) / 1000 for b in map(_bar, bars))


def _bounds(value: Any, label: str) -> tuple[float, float, float, float]:
    try:
        result = tuple(map(float, value))
    except (TypeError, ValueError) as exc:
        raise LayoutMetricError(f"{label}: отсутствуют границы зоны") from exc
    if len(result) != 4 or not all(map(isfinite, result)) or result[2] < result[0] or result[3] < result[1]:
        raise LayoutMetricError(f"{label}: некорректные границы {result}")
    return result


def _trim(bars: Sequence[Any], bounds: Sequence[float], axis: str) -> list[tuple]:
    lo_i, hi_i = (1, 3) if axis == "y" else (0, 2)
    out = []
    for raw in bars:
        b = list(_bar(raw))
        lo = max(min(b[lo_i], b[hi_i]), bounds[lo_i])
        hi = min(max(b[lo_i], b[hi_i]), bounds[hi_i])
        if hi > lo:
            b[lo_i], b[hi_i] = lo, hi
            out.append(tuple(b))
    return out


def _envelope(bars: Sequence[Any], fallback: tuple) -> tuple:
    if not bars:
        return fallback
    rows = list(map(_bar, bars))
    return (min(min(b[0], b[2]) for b in rows), min(min(b[1], b[3]) for b in rows),
            max(max(b[0], b[2]) for b in rows), max(max(b[1], b[3]) for b in rows))


def _check_order(m: Mapping[str, float], label: str) -> None:
    for upper, lower in (
        ("with_anchorage_unclipped_kg", "with_anchorage_kg"),
        ("with_anchorage_unclipped_kg", "without_anchorage_unclipped_kg"),
        ("without_anchorage_unclipped_kg", "without_anchorage_kg"),
        ("with_anchorage_kg", "without_anchorage_kg"),
    ):
        if m[upper] + max(1e-7, abs(m[lower]) * 1e-9) < m[lower]:
            raise LayoutMetricError(f"{label}: {upper}={m[upper]} меньше {lower}={m[lower]}")


def augment_layout_mass_metrics(
    layout: Mapping[str, Any], *, steel_density_kg_m3: float = 7850.0,
    anchor_factor: float | None = None,
) -> dict[str, Any]:
    """Enrich a layout without changing canonical physical ``bars`` or solver N.

    ``unclipped`` extends each allocated supplemental track to the full fitted
    rectangle, optionally plus d * anchor_factor at both longitudinal ends.
    It does NOT invent a different transverse grid or unclip background rods.
    A separate track remains a separate rod even at the same coordinate.
    """
    from A101.reinforcement_components import bar_mass_kg

    density = float(steel_density_kg_m3)
    if not isfinite(density) or density <= 0:
        raise LayoutMetricError("Плотность стали должна быть конечной и положительной")
    if anchor_factor is not None and (not isfinite(float(anchor_factor)) or float(anchor_factor) < 0):
        raise LayoutMetricError("anchor_factor должен быть конечным и неотрицательным")
    out = dict(layout or {})
    axis = str(out.get("axis", "y")).lower()
    if axis not in {"x", "y"}:
        raise LayoutMetricError("axis должен быть x или y")
    long0, long1, cross0, cross1 = (1, 3, 0, 2) if axis == "y" else (0, 2, 1, 3)
    zones = [dict(z) for z in out.get("zones", []) or []]
    tracks = {int(t["id"]): t for t in out.get("tracks", []) or [] if t.get("id") is not None}
    all_bars = list(out.get("bars", []) or [])
    background = [b for b in all_bars if isinstance(b, Mapping) and b.get("background")]
    background_mass = bar_mass_kg(background, density) if background else 0.0
    by_zone: dict[Any, list] = defaultdict(list)
    for b in all_bars:
        if isinstance(b, Mapping) and not b.get("background"):
            by_zone[b.get("zone_id")].append(b)
    additional_parts = {key: [] for key in _MASS_KEYS}

    for zone in zones:
        if zone.get("background"):
            continue
        diameter = float(zone.get("diameter") or 0)
        if not isfinite(diameter) or diameter <= 0:
            raise LayoutMetricError(f"zone={zone.get('id')}: отсутствует положительный diameter")
        clipped_bounds = _bounds(zone.get("bounds") or zone.get("primary_bounds"), "bounds")
        fitted = _bounds(zone.get("fitted_bounds") or zone.get("primary_bounds") or clipped_bounds, "fitted_bounds")
        hold = float(anchor_factor) * diameter if anchor_factor is not None else zone.get("hold")
        if hold is not None:
            hold = float(hold)
            if not isfinite(hold) or hold < 0:
                raise LayoutMetricError("hold должен быть конечным и неотрицательным")
            full = list(fitted)
            full[long0] -= hold
            full[long1] += hold
            uncut = tuple(full)
        else:
            uncut = _bounds(zone.get("anchored_bounds_unclipped") or clipped_bounds, "anchored_bounds_unclipped")
        if uncut[long0] > fitted[long0] + _EPS or uncut[long1] < fitted[long1] - _EPS:
            raise LayoutMetricError(f"zone={zone.get('id')}: анкеровка не содержит fitted_bounds; требуется повторный layout")

        clipped = list(map(_bar, zone.get("bars", []) or []))
        no_anchor = _trim(clipped, fitted, axis)
        records = by_zone.get(zone.get("id"), [])
        records_by_track: dict[int, list] = defaultdict(list)
        for b in records:
            if b.get("track_id") is not None:
                records_by_track[int(b["track_id"])].append(b)
        ids = list(dict.fromkeys(int(t) for t in zone.get("track_ids", []) or []))
        for tid in records_by_track:
            if tid not in ids:
                ids.append(tid)
        coordinates = []
        uncut_track_ids = []
        for tid in ids:
            track = tracks.get(tid, {})
            coord = track.get("x" if axis == "y" else "y")
            if coord is None and records_by_track.get(tid):
                coord = _bar(records_by_track[tid][0])[cross0]
            if coord is None:
                raise LayoutMetricError(f"zone={zone.get('id')}, track={tid}: нет координаты стержня")
            coordinates.append(float(coord))
            uncut_track_ids.append(tid)
        if not ids and clipped:
            raise LayoutMetricError(
                f"zone={zone.get('id')}: отсутствуют track_id; нужен повторный layout, "
                "а не восстановление кратности по одной координате"
            )
        zone["mass_metric_provenance"] = "track_id"
        if not all(map(isfinite, coordinates)):
            raise LayoutMetricError("Неконечная координата стержня")
        for b in clipped:
            if min(b[long0], b[long1]) < uncut[long0] - _EPS or max(b[long0], b[long1]) > uncut[long1] + _EPS:
                raise LayoutMetricError(f"zone={zone.get('id')}: физический стержень выходит за неподрезанную анкеровку")

        # The allocator can shift tracks laterally. The diagnostic rectangle
        # must contain those SAME coordinates, not a newly centred grid.
        no_anchor_full_bounds = list(fitted)
        anchored_full_bounds = list(uncut)
        if coordinates:
            for bounds in (no_anchor_full_bounds, anchored_full_bounds):
                bounds[cross0] = min(bounds[cross0], clipped_bounds[cross0], min(coordinates))
                bounds[cross1] = max(bounds[cross1], clipped_bounds[cross1], max(coordinates))
        def full_bars(bounds: Sequence[float]) -> list[tuple]:
            if axis == "y":
                return [(c, bounds[1], c, bounds[3]) for c in coordinates]
            return [(bounds[0], c, bounds[2], c) for c in coordinates]
        no_anchor_uncut = full_bars(no_anchor_full_bounds)
        anchored_uncut = full_bars(anchored_full_bounds)
        masses = dict(zip(_MASS_KEYS, (
            _mass(no_anchor, diameter, density), _mass(clipped, diameter, density),
            _mass(no_anchor_uncut, diameter, density), _mass(anchored_uncut, diameter, density),
        )))
        _check_order(masses, f"zone={zone.get('id')}")
        for key in _MASS_KEYS:
            additional_parts[key].append(masses[key])
        # Preserve the layout's transverse zone boundaries; longitudinal bounds
        # without anchorage come from actual clipped rods, not a raw alias.
        no_anchor_bounds = list(clipped_bounds)
        env = _envelope(no_anchor, fitted)
        no_anchor_bounds[long0], no_anchor_bounds[long1] = env[long0], env[long1]
        zone.update({
            "final_rectangle_without_anchorage": tuple(no_anchor_bounds),
            "final_rectangle_with_anchorage": clipped_bounds,
            "final_rectangle_without_anchorage_unclipped": tuple(no_anchor_full_bounds),
            "final_rectangle_with_anchorage_unclipped": tuple(anchored_full_bounds),
            "anchored_bounds_unclipped": tuple(anchored_full_bounds),
            "bars_without_anchorage": no_anchor, "bars_with_anchorage": clipped,
            "bars_without_anchorage_unclipped": no_anchor_uncut,
            "bars_with_anchorage_unclipped": anchored_uncut,
            "unclipped_track_ids": uncut_track_ids,
            "zone_mass_without_anchorage_kg": masses["without_anchorage_kg"],
            "zone_mass_with_anchorage_kg": masses["with_anchorage_kg"],
            "zone_mass_without_anchorage_unclipped_kg": masses["without_anchorage_unclipped_kg"],
            "zone_mass_with_anchorage_unclipped_kg": masses["with_anchorage_unclipped_kg"],
        })
    additional = {k: fsum(v) for k, v in additional_parts.items()}
    totals = {k: background_mass + additional[k] for k in _MASS_KEYS}
    canonical = bar_mass_kg(all_bars, density) if all_bars else 0.0
    if not isclose(totals["with_anchorage_kg"], canonical, rel_tol=1e-9, abs_tol=1e-7):
        raise LayoutMetricError(f"Масса зон+фона {totals['with_anchorage_kg']} не совпадает с массой стержней {canonical}")
    _check_order(totals, "layout")
    out["zones"] = zones
    out["mass_metrics"] = {
        **totals, "schema_version": MASS_METRICS_VERSION,
        "unclipped_scope": "allocated_supplemental_tracks_background_clipped",
        "anchorage_kg": totals["with_anchorage_kg"] - totals["without_anchorage_kg"],
        "anchorage_unclipped_kg": totals["with_anchorage_unclipped_kg"] - totals["without_anchorage_unclipped_kg"],
        "additional": additional, "background_clipped_kg": background_mass,
    }
    return out
