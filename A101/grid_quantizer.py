from __future__ import annotations

__version__ = "0.5.0"

from collections import defaultdict
from dataclasses import dataclass
import heapq
import math
from typing import Iterable, Iterator, Mapping, Any

import numpy as np
import shapely
from numba import njit
from shapely import STRtree
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union


@dataclass(frozen=True)
class AxisEvents:
    """Aggregated costs, movement limits, and topology constraints."""

    coords: np.ndarray
    w_minus: np.ndarray
    w_plus: np.ndarray
    tol_minus: np.ndarray
    tol_plus: np.ndarray
    # For a cluster starting at index i, its right endpoint may not be
    # greater than max_cluster_right[i]. This encodes protected gaps such
    # as shell -> hole -> shell and prevents holes from collapsing.
    max_cluster_right: np.ndarray | None = None
    protected_spans: np.ndarray | None = None


def _iter_polygons(geometry) -> Iterator[Polygon]:
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type in ("MultiPolygon", "GeometryCollection"):
        for part in geometry.geoms:
            yield from _iter_polygons(part)


def _polygonal_only(geometry):
    polygons = list(_iter_polygons(geometry))
    if not polygons:
        return Polygon()
    return unary_union(polygons)


def _canonical(value: float, eps: float) -> float:
    if eps <= 0:
        return float(value)
    return float(round(float(value) / eps) * eps)


def _prepare_records(
    records: Iterable[Mapping[str, Any]],
    merge_equal_loads: bool,
):
    cleaned: list[tuple[Any, float]] = []
    for record in records:
        geometry = record["geometry"]
        load = float(record["load"])
        if geometry is None or geometry.is_empty:
            continue
        if not np.isfinite(load) or load < 0:
            raise ValueError("Every load must be a finite non-negative number.")
        cleaned.append((geometry, load))

    if not cleaned:
        raise ValueError("No non-empty geometries were supplied.")

    if not merge_equal_loads:
        return cleaned

    groups: dict[float, list[Any]] = defaultdict(list)
    for geometry, load in cleaned:
        groups[load].append(geometry)

    return [(unary_union(geometries), load) for load, geometries in groups.items()]


def build_axis_events(
    records: Iterable[Mapping[str, Any]],
    *,
    coord_eps: float = 1e-8,
    max_shift_fraction: float = 0.02,
    shrink_penalty: float = 20.0,
    expand_penalty: float = 1.0,
    load_gamma: float = 2.0,
    base_priority: float = 0.03,
    min_shrink_tol_ratio: float = 0.10,
    min_expand_tol_ratio: float = 0.50,
    load_scale: float | None = None,
    merge_equal_loads: bool = True,
    preserve_holes: bool = True,
    min_hole_area: float = 0.0,
    min_hole_area_fraction: float = 0.0,
) -> tuple[AxisEvents, AxisEvents]:
    """
    Convert polygon edges into asymmetric line-movement costs.

    A movement toward a polygon interior shrinks it and receives
    ``shrink_penalty``. A movement away from the interior expands it and
    receives ``expand_penalty``. Both costs are multiplied by edge length
    and by a monotone priority derived from ``load``.

    ``max_shift_fraction`` is relative to the complete span of each axis.
    The highest-load polygons receive:
      - inward tolerance = max_shift * min_shrink_tol_ratio
      - outward tolerance = max_shift * min_expand_tol_ratio

    When ``preserve_holes`` is true, significant holes add hard protected
    coordinate spans. Their opposite sides and their separation from the
    exterior shell cannot be collapsed into one quantized line.
    """

    if not (0 < max_shift_fraction):
        raise ValueError("max_shift_fraction must be positive.")
    if not (0 < min_shrink_tol_ratio <= 1):
        raise ValueError("min_shrink_tol_ratio must be in (0, 1].")
    if not (0 < min_expand_tol_ratio <= 1):
        raise ValueError("min_expand_tol_ratio must be in (0, 1].")
    if shrink_penalty <= 0 or expand_penalty <= 0:
        raise ValueError("Penalties must be positive.")
    if min_hole_area < 0 or min_hole_area_fraction < 0:
        raise ValueError("Hole area thresholds must be non-negative.")

    records = list(records)
    cleaned = _prepare_records(records, merge_equal_loads=False)
    prepared = _prepare_records(records, merge_equal_loads)

    bounds = np.asarray([geometry.bounds for geometry, _ in prepared], dtype=float)
    min_x, min_y = np.min(bounds[:, 0]), np.min(bounds[:, 1])
    max_x, max_y = np.max(bounds[:, 2]), np.max(bounds[:, 3])
    span_x, span_y = max_x - min_x, max_y - min_y
    if span_x <= 0 or span_y <= 0:
        raise ValueError("The total polygon extent must have positive width and height.")

    max_shift_x = span_x * max_shift_fraction
    max_shift_y = span_y * max_shift_fraction

    loads = np.asarray([load for _, load in prepared], dtype=float)
    if load_scale is None:
        positive = loads[loads > 0]
        load_scale = float(np.quantile(positive, 0.95)) if positive.size else 1.0
    load_scale = max(float(load_scale), np.finfo(float).eps)

    # Event value:
    # [cost for negative move, cost for positive move,
    #  tolerance for negative move, tolerance for positive move]
    x_events: dict[float, list[float]] = defaultdict(
        lambda: [0.0, 0.0, math.inf, math.inf]
    )
    y_events: dict[float, list[float]] = defaultdict(
        lambda: [0.0, 0.0, math.inf, math.inf]
    )

    # Coordinate pairs that must be assigned to different clusters. For
    # every protected hole, preserve one grid strip between the exterior
    # shell and the hole on both sides, plus at least one strip inside the
    # hole itself. This is a hard topology constraint, not a soft cost.
    x_protected_values: list[tuple[float, float]] = []
    y_protected_values: list[tuple[float, float]] = []

    def add_protected_span(
        store: list[tuple[float, float]],
        first: float,
        second: float,
    ) -> None:
        first = _canonical(first, coord_eps)
        second = _canonical(second, coord_eps)
        if second < first:
            first, second = second, first
        if second - first > coord_eps:
            store.append((first, second))

    if preserve_holes:
        for geometry, _ in cleaned:
            for polygon_0 in _iter_polygons(geometry):
                polygon = orient(polygon_0, sign=1.0)
                shell_min_x, shell_min_y, shell_max_x, shell_max_y = polygon.bounds
                area_threshold = max(
                    float(min_hole_area),
                    float(min_hole_area_fraction) * float(polygon.area),
                )

                for ring in polygon.interiors:
                    hole_polygon = Polygon(ring)
                    if hole_polygon.is_empty or hole_polygon.area + 1e-12 < area_threshold:
                        continue

                    hole_min_x, hole_min_y, hole_max_x, hole_max_y = hole_polygon.bounds

                    add_protected_span(x_protected_values, shell_min_x, hole_min_x)
                    add_protected_span(x_protected_values, hole_min_x, hole_max_x)
                    add_protected_span(x_protected_values, hole_max_x, shell_max_x)

                    add_protected_span(y_protected_values, shell_min_y, hole_min_y)
                    add_protected_span(y_protected_values, hole_min_y, hole_max_y)
                    add_protected_span(y_protected_values, hole_max_y, shell_max_y)

    def add_event(
        store: dict[float, list[float]],
        coordinate: float,
        edge_length: float,
        interior_is_positive: bool,
        relative_load: float,
        axis_max_shift: float,
    ) -> None:
        load_term = relative_load**load_gamma
        priority = base_priority + load_term

        shrink_rate = edge_length * priority * shrink_penalty
        expand_rate = edge_length * priority * expand_penalty

        shrink_tolerance = axis_max_shift * (
            1.0 - (1.0 - min_shrink_tol_ratio) * load_term
        )
        expand_tolerance = axis_max_shift * (
            1.0 - (1.0 - min_expand_tol_ratio) * load_term
        )

        event = store[coordinate]
        if interior_is_positive:
            # Positive move is inward (shrink); negative move is outward.
            event[0] += expand_rate
            event[1] += shrink_rate
            event[2] = min(event[2], expand_tolerance)
            event[3] = min(event[3], shrink_tolerance)
        else:
            # Negative move is inward (shrink); positive move is outward.
            event[0] += shrink_rate
            event[1] += expand_rate
            event[2] = min(event[2], shrink_tolerance)
            event[3] = min(event[3], expand_tolerance)

    for geometry, load in prepared:
        relative_load = float(np.clip(load / load_scale, 0.0, 1.0))

        for polygon_0 in _iter_polygons(geometry):
            # Exterior CCW, holes CW. Polygon interior is on the left side
            # of every directed ring segment.
            polygon = orient(polygon_0, sign=1.0)

            for ring in [polygon.exterior, *polygon.interiors]:
                coordinates = list(ring.coords)
                for first, second in zip(coordinates[:-1], coordinates[1:]):
                    x1 = _canonical(first[0], coord_eps)
                    y1 = _canonical(first[1], coord_eps)
                    x2 = _canonical(second[0], coord_eps)
                    y2 = _canonical(second[1], coord_eps)
                    dx, dy = x2 - x1, y2 - y1

                    if abs(dx) <= coord_eps and abs(dy) > coord_eps:
                        x = _canonical((x1 + x2) / 2.0, coord_eps)
                        # Downward edge: interior is east (+x).
                        interior_is_positive = dy < 0
                        add_event(
                            x_events,
                            x,
                            abs(dy),
                            interior_is_positive,
                            relative_load,
                            max_shift_x,
                        )
                    elif abs(dy) <= coord_eps and abs(dx) > coord_eps:
                        y = _canonical((y1 + y2) / 2.0, coord_eps)
                        # Rightward edge: interior is north (+y).
                        interior_is_positive = dx > 0
                        add_event(
                            y_events,
                            y,
                            abs(dx),
                            interior_is_positive,
                            relative_load,
                            max_shift_y,
                        )
                    elif abs(dx) <= coord_eps and abs(dy) <= coord_eps:
                        continue
                    else:
                        raise ValueError(
                            "A non-orthogonal edge was found: "
                            f"({x1}, {y1}) -> ({x2}, {y2})."
                        )

    # Merging equal-load polygons can remove their internal boundaries from
    # the weighted union. Those coordinates still occur in the original
    # records and must remain mappable when each source geometry is snapped.
    # Zero-weight events are free to merge, so this does not force extra grid
    # lines; it only keeps the coordinate map complete.
    for geometry, _ in cleaned:
        for polygon_0 in _iter_polygons(geometry):
            polygon = orient(polygon_0, sign=1.0)
            for ring in [polygon.exterior, *polygon.interiors]:
                coordinates = list(ring.coords)
                for first, second in zip(coordinates[:-1], coordinates[1:]):
                    x1 = _canonical(first[0], coord_eps)
                    y1 = _canonical(first[1], coord_eps)
                    x2 = _canonical(second[0], coord_eps)
                    y2 = _canonical(second[1], coord_eps)
                    dx, dy = x2 - x1, y2 - y1
                    if abs(dx) <= coord_eps and abs(dy) > coord_eps:
                        x_events[_canonical((x1 + x2) / 2.0, coord_eps)]
                    elif abs(dy) <= coord_eps and abs(dx) > coord_eps:
                        y_events[_canonical((y1 + y2) / 2.0, coord_eps)]

    def finalize(
        store: dict[float, list[float]],
        minimum: float,
        maximum: float,
        protected_values: list[tuple[float, float]],
    ) -> AxisEvents:
        coords = np.asarray(sorted(store), dtype=float)
        w_minus = np.asarray([store[c][0] for c in coords], dtype=float)
        w_plus = np.asarray([store[c][1] for c in coords], dtype=float)
        tol_minus = np.asarray([store[c][2] for c in coords], dtype=float)
        tol_plus = np.asarray([store[c][3] for c in coords], dtype=float)

        first = int(np.argmin(np.abs(coords - minimum)))
        last = int(np.argmin(np.abs(coords - maximum)))
        allowed_error = max(coord_eps * 2.0, 1e-10)
        if (
            abs(coords[first] - minimum) > allowed_error
            or abs(coords[last] - maximum) > allowed_error
        ):
            raise RuntimeError("The outer bounds were not found among edge events.")

        # The complete field extent is immutable.
        tol_minus[first] = tol_plus[first] = 0.0
        tol_minus[last] = tol_plus[last] = 0.0

        protected_indices: list[tuple[int, int]] = []
        for value_a, value_b in protected_values:
            index_a = int(np.argmin(np.abs(coords - value_a)))
            index_b = int(np.argmin(np.abs(coords - value_b)))
            if (
                abs(coords[index_a] - value_a) > allowed_error
                or abs(coords[index_b] - value_b) > allowed_error
            ):
                raise RuntimeError(
                    "A protected hole boundary was not found among edge events."
                )
            if index_b < index_a:
                index_a, index_b = index_b, index_a
            if index_a < index_b:
                protected_indices.append((index_a, index_b))

        if protected_indices:
            protected_spans = np.asarray(
                sorted(set(protected_indices)),
                dtype=np.int32,
            ).reshape(-1, 2)
        else:
            protected_spans = np.empty((0, 2), dtype=np.int32)

        # A segment [left, right] is invalid if it contains both endpoints
        # of any protected span. For every possible left endpoint, store the
        # largest allowed right endpoint.
        n = len(coords)
        earliest_protected_end = np.full(n, n, dtype=np.int32)
        for index_a, index_b in protected_spans:
            earliest_protected_end[index_a] = min(
                earliest_protected_end[index_a],
                index_b,
            )
        suffix_earliest_end = np.minimum.accumulate(
            earliest_protected_end[::-1]
        )[::-1]
        max_cluster_right = np.where(
            suffix_earliest_end < n,
            suffix_earliest_end - 1,
            n - 1,
        ).astype(np.int32)

        return AxisEvents(
            coords,
            w_minus,
            w_plus,
            tol_minus,
            tol_plus,
            max_cluster_right,
            protected_spans,
        )

    return (
        finalize(x_events, min_x, max_x, x_protected_values),
        finalize(y_events, min_y, max_y, y_protected_values),
    )


class _AxisCostModel:
    """O(log n) cost query for one contiguous coordinate cluster."""

    def __init__(self, events: AxisEvents):
        self.events = events
        self.x = events.coords
        self.a = events.w_minus
        self.b = events.w_plus

        self.prefix_a = np.r_[0.0, np.cumsum(self.a)]
        self.prefix_b = np.r_[0.0, np.cumsum(self.b)]
        self.prefix_ax = np.r_[0.0, np.cumsum(self.a * self.x)]
        self.prefix_bx = np.r_[0.0, np.cumsum(self.b * self.x)]
        self.prefix_jump = np.r_[0.0, np.cumsum(self.a + self.b)]

    def segment_with_bounds(
        self,
        left: int,
        right: int,
        raw_lower: float,
        raw_upper: float,
    ) -> tuple[float, float]:
        lower = max(float(self.x[left]), float(raw_lower))
        upper = min(float(self.x[right]), float(raw_upper))
        if lower > upper + 1e-12:
            return math.inf, math.nan

        total_negative_slope = self.prefix_a[right + 1] - self.prefix_a[left]
        if total_negative_slope <= 0:
            unconstrained = self.x[left]
        else:
            target = self.prefix_jump[left] + total_negative_slope
            prefix_index = int(np.searchsorted(self.prefix_jump, target, side="left"))
            median_index = max(left, min(right, prefix_index - 1))
            unconstrained = self.x[median_index]

        representative = float(np.clip(unconstrained, lower, upper))

        split = int(np.searchsorted(self.x, representative, side="left"))
        split = max(left, min(right + 1, split))

        left_cost = (
            representative * (self.prefix_b[split] - self.prefix_b[left])
            - (self.prefix_bx[split] - self.prefix_bx[left])
        )
        right_cost = (
            self.prefix_ax[right + 1]
            - self.prefix_ax[split]
            - representative
            * (self.prefix_a[right + 1] - self.prefix_a[split])
        )
        return max(0.0, float(left_cost + right_cost)), representative

    def segment(self, left: int, right: int) -> tuple[float, float]:
        raw_lower = float(
            np.max(
                self.x[left : right + 1]
                - self.events.tol_minus[left : right + 1]
            )
        )
        raw_upper = float(
            np.min(
                self.x[left : right + 1]
                + self.events.tol_plus[left : right + 1]
            )
        )
        return self.segment_with_bounds(left, right, raw_lower, raw_upper)


def _max_cluster_right(events: AxisEvents) -> np.ndarray:
    if events.max_cluster_right is None:
        return np.full(len(events.coords), len(events.coords) - 1, dtype=np.int32)
    return np.asarray(events.max_cluster_right, dtype=np.int32)


def minimum_feasible_grid_lines(events: AxisEvents) -> int:
    """Minimum line count allowed by movement and topology constraints."""

    lower = events.coords - events.tol_minus
    upper = events.coords + events.tol_plus
    max_right = _max_cluster_right(events)
    n = len(events.coords)

    groups = 0
    start = 0
    while start < n:
        groups += 1
        intersection_lower = float(lower[start])
        intersection_upper = float(upper[start])
        right = start
        right_limit = int(max_right[start])

        while right + 1 < n and right + 1 <= right_limit:
            candidate = right + 1
            next_lower = max(intersection_lower, float(lower[candidate]))
            next_upper = min(intersection_upper, float(upper[candidate]))
            if next_lower > next_upper + 1e-12:
                break
            intersection_lower = next_lower
            intersection_upper = next_upper
            right = candidate

        start = right + 1

    return groups


@njit(cache=True)
def _precompute_segment_costs(
    x: np.ndarray,
    a: np.ndarray,
    b: np.ndarray,
    tol_minus: np.ndarray,
    tol_plus: np.ndarray,
    max_cluster_right: np.ndarray,
) -> np.ndarray:
    n = x.size
    prefix_a = np.empty(n + 1)
    prefix_b = np.empty(n + 1)
    prefix_ax = np.empty(n + 1)
    prefix_bx = np.empty(n + 1)
    prefix_jump = np.empty(n + 1)

    prefix_a[0] = 0.0
    prefix_b[0] = 0.0
    prefix_ax[0] = 0.0
    prefix_bx[0] = 0.0
    prefix_jump[0] = 0.0

    for i in range(n):
        prefix_a[i + 1] = prefix_a[i] + a[i]
        prefix_b[i + 1] = prefix_b[i] + b[i]
        prefix_ax[i + 1] = prefix_ax[i] + a[i] * x[i]
        prefix_bx[i + 1] = prefix_bx[i] + b[i] * x[i]
        prefix_jump[i + 1] = prefix_jump[i] + a[i] + b[i]

    costs = np.full((n, n), np.inf)

    for left in range(n):
        raw_lower = -np.inf
        raw_upper = np.inf

        for right in range(left, n):
            if right > max_cluster_right[left]:
                break
            raw_lower = max(raw_lower, x[right] - tol_minus[right])
            raw_upper = min(raw_upper, x[right] + tol_plus[right])

            lower = max(x[left], raw_lower)
            upper = min(x[right], raw_upper)
            if lower > upper + 1e-12:
                continue

            total_negative_slope = prefix_a[right + 1] - prefix_a[left]
            if total_negative_slope <= 0:
                unconstrained = x[left]
            else:
                target = prefix_jump[left] + total_negative_slope
                lo = left + 1
                hi = right + 2
                while lo < hi:
                    mid = (lo + hi) // 2
                    if prefix_jump[mid] < target:
                        lo = mid + 1
                    else:
                        hi = mid
                median_index = min(right, max(left, lo - 1))
                unconstrained = x[median_index]

            representative = min(upper, max(lower, unconstrained))

            lo = left
            hi = right + 1
            while lo < hi:
                mid = (lo + hi) // 2
                if x[mid] < representative:
                    lo = mid + 1
                else:
                    hi = mid
            split = lo

            left_cost = (
                representative * (prefix_b[split] - prefix_b[left])
                - (prefix_bx[split] - prefix_bx[left])
            )
            right_cost = (
                prefix_ax[right + 1]
                - prefix_ax[split]
                - representative
                * (prefix_a[right + 1] - prefix_a[split])
            )
            cost = left_cost + right_cost
            if -1e-8 < cost < 0:
                cost = 0.0
            costs[left, right] = cost

    return costs


@njit(cache=True)
def _exact_partition(costs: np.ndarray, number_of_clusters: int):
    n = costs.shape[0]
    previous = np.full(n + 1, np.inf)
    previous[0] = 0.0
    back = np.full((number_of_clusters + 1, n + 1), -1, np.int32)

    for cluster_count in range(1, number_of_clusters + 1):
        current = np.full(n + 1, np.inf)

        for end_exclusive in range(cluster_count, n + 1):
            best = np.inf
            best_start = -1

            for start in range(cluster_count - 1, end_exclusive):
                value = (
                    previous[start]
                    + costs[start, end_exclusive - 1]
                )
                if value < best:
                    best = value
                    best_start = start

            current[end_exclusive] = best
            back[cluster_count, end_exclusive] = best_start

        previous = current

    if not np.isfinite(previous[n]):
        return previous[n], np.empty(0, np.int32)

    cuts = np.empty(number_of_clusters + 1, np.int32)
    cuts[number_of_clusters] = n
    end_exclusive = n

    for cluster_count in range(number_of_clusters, 0, -1):
        start = back[cluster_count, end_exclusive]
        cuts[cluster_count - 1] = start
        end_exclusive = start

    return previous[n], cuts


@njit(cache=True)
def _exact_partition_frontier(costs: np.ndarray, max_clusters: int):
    """Solve the exact partition DP once for every cluster count up to K."""

    n = costs.shape[0]
    max_clusters = min(int(max_clusters), n)
    previous = np.full(n + 1, np.inf)
    previous[0] = 0.0
    objectives = np.full(max_clusters + 1, np.inf)
    back = np.full((max_clusters + 1, n + 1), -1, np.int32)

    for cluster_count in range(1, max_clusters + 1):
        current = np.full(n + 1, np.inf)

        for end_exclusive in range(cluster_count, n + 1):
            best = np.inf
            best_start = -1

            for start in range(cluster_count - 1, end_exclusive):
                value = previous[start] + costs[start, end_exclusive - 1]
                if value < best:
                    best = value
                    best_start = start

            current[end_exclusive] = best
            back[cluster_count, end_exclusive] = best_start

        objectives[cluster_count] = current[n]
        previous = current

    return objectives, back


def _exact_result_from_frontier(
    events: AxisEvents,
    target_cells: int,
    objectives: np.ndarray,
    back: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
    """Materialize one target from a previously computed exact frontier."""

    n = len(events.coords)
    target_lines = int(target_cells) + 1
    if target_lines >= n:
        return (
            events.coords.copy(),
            events.coords.copy(),
            0.0,
            np.arange(n + 1, dtype=np.int32),
        )
    if target_lines < 2:
        raise ValueError("target_cells must be at least 1.")
    if target_lines >= len(objectives) or not np.isfinite(objectives[target_lines]):
        raise RuntimeError("No feasible exact partition was found in the frontier.")

    cuts = np.empty(target_lines + 1, dtype=np.int32)
    cuts[target_lines] = n
    end_exclusive = n
    for cluster_count in range(target_lines, 0, -1):
        start = int(back[cluster_count, end_exclusive])
        if start < 0:
            raise RuntimeError("The exact frontier has an incomplete backtrace.")
        cuts[cluster_count - 1] = start
        end_exclusive = start

    model = _AxisCostModel(events)
    mapping = np.empty(n, dtype=float)
    representatives: list[float] = []
    for cluster in range(target_lines):
        left = int(cuts[cluster])
        right = int(cuts[cluster + 1] - 1)
        _, representative = model.segment(left, right)
        representatives.append(representative)
        mapping[left : right + 1] = representative

    return (
        np.asarray(representatives, dtype=float),
        mapping,
        float(objectives[target_lines]),
        cuts,
    )


def quantize_axis_exact(
    events: AxisEvents,
    target_cells: int,
) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
    """
    Exact dynamic programming solver.

    Returns:
        quantized grid lines,
        mapping for every original line,
        objective value,
        cluster cuts.
    """

    n = len(events.coords)
    target_lines = int(target_cells) + 1

    if target_lines >= n:
        return (
            events.coords.copy(),
            events.coords.copy(),
            0.0,
            np.arange(n + 1, dtype=np.int32),
        )
    if target_lines < 2:
        raise ValueError("target_cells must be at least 1.")

    minimum_lines = minimum_feasible_grid_lines(events)
    if target_lines < minimum_lines:
        raise ValueError(
            f"Requested {target_cells} cells, but hard movement/topology "
            f"constraints require at least {minimum_lines - 1} cells "
            "on this axis."
        )

    costs = _precompute_segment_costs(
        events.coords,
        events.w_minus,
        events.w_plus,
        events.tol_minus,
        events.tol_plus,
        _max_cluster_right(events),
    )
    objective, cuts = _exact_partition(costs, target_lines)
    if not np.isfinite(objective):
        raise RuntimeError("No feasible exact partition was found.")

    model = _AxisCostModel(events)
    mapping = np.empty(n, dtype=float)
    representatives: list[float] = []

    for cluster in range(target_lines):
        left = int(cuts[cluster])
        right = int(cuts[cluster + 1] - 1)
        _, representative = model.segment(left, right)
        representatives.append(representative)
        mapping[left : right + 1] = representative

    return (
        np.asarray(representatives, dtype=float),
        mapping,
        float(objective),
        cuts,
    )


def quantize_axis_greedy(
    events: AxisEvents,
    target_cells: int,
) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
    """
    Fast adjacent-cluster merging.

    This is not globally optimal, but typically scales to many more
    unique coordinates than the exact O(K*N^2) dynamic program.
    """

    n = len(events.coords)
    target_lines = int(target_cells) + 1

    if target_lines >= n:
        return (
            events.coords.copy(),
            events.coords.copy(),
            0.0,
            np.arange(n + 1, dtype=np.int32),
        )
    if target_lines < 2:
        raise ValueError("target_cells must be at least 1.")

    minimum_lines = minimum_feasible_grid_lines(events)
    if target_lines < minimum_lines:
        raise ValueError(
            f"Requested {target_cells} cells, but hard movement/topology "
            f"constraints require at least {minimum_lines - 1} cells "
            "on this axis."
        )

    model = _AxisCostModel(events)
    max_cluster_right = _max_cluster_right(events)

    left_index = np.arange(n, dtype=np.int32)
    right_index = np.arange(n, dtype=np.int32)
    raw_lower = events.coords - events.tol_minus
    raw_upper = events.coords + events.tol_plus
    cluster_cost = np.zeros(n, dtype=float)

    previous = np.arange(n, dtype=np.int32) - 1
    following = np.arange(n, dtype=np.int32) + 1
    following[-1] = -1

    active = np.ones(n, dtype=bool)
    version = np.zeros(n, dtype=np.int64)
    heap: list[tuple] = []

    def push_candidate(first: int, second: int) -> None:
        if (
            first < 0
            or second < 0
            or not active[first]
            or not active[second]
            or following[first] != second
        ):
            return

        merged_left = int(left_index[first])
        merged_right = int(right_index[second])
        if merged_right > int(max_cluster_right[merged_left]):
            return

        merged_lower = max(raw_lower[first], raw_lower[second])
        merged_upper = min(raw_upper[first], raw_upper[second])
        merged_cost, _ = model.segment_with_bounds(
            int(left_index[first]),
            int(right_index[second]),
            float(merged_lower),
            float(merged_upper),
        )
        if np.isfinite(merged_cost):
            increase = max(
                0.0,
                merged_cost - cluster_cost[first] - cluster_cost[second],
            )
            heapq.heappush(
                heap,
                (
                    increase,
                    first,
                    second,
                    int(version[first]),
                    int(version[second]),
                    merged_cost,
                    merged_lower,
                    merged_upper,
                ),
            )

    for index in range(n - 1):
        push_candidate(index, index + 1)

    cluster_count = n
    while cluster_count > target_lines and heap:
        (
            _,
            first,
            second,
            version_first,
            version_second,
            merged_cost,
            merged_lower,
            merged_upper,
        ) = heapq.heappop(heap)

        if (
            not active[first]
            or not active[second]
            or following[first] != second
            or version[first] != version_first
            or version[second] != version_second
        ):
            continue

        right_index[first] = right_index[second]
        raw_lower[first] = merged_lower
        raw_upper[first] = merged_upper
        cluster_cost[first] = merged_cost

        after = following[second]
        following[first] = after
        if after >= 0:
            previous[after] = first

        active[second] = False
        version[first] += 1
        version[second] += 1
        cluster_count -= 1

        push_candidate(int(previous[first]), first)
        push_candidate(first, int(following[first]))

    if cluster_count > target_lines:
        raise RuntimeError(
            "The greedy merge order could not reach the requested size. "
            "Use method='exact' or relax the hard shift tolerances."
        )

    mapping = np.empty(n, dtype=float)
    representatives: list[float] = []
    cuts = [0]
    objective = 0.0

    cluster = 0
    while cluster >= 0:
        cost, representative = model.segment_with_bounds(
            int(left_index[cluster]),
            int(right_index[cluster]),
            float(raw_lower[cluster]),
            float(raw_upper[cluster]),
        )
        representatives.append(representative)
        mapping[
            left_index[cluster] : right_index[cluster] + 1
        ] = representative
        objective += cost
        cuts.append(int(right_index[cluster]) + 1)
        cluster = int(following[cluster])

    return (
        np.asarray(representatives, dtype=float),
        mapping,
        float(objective),
        np.asarray(cuts, dtype=np.int32),
    )


def _greedy_objective_frontier(
    events: AxisEvents,
    target_cells_values: Iterable[int],
) -> dict[int, float]:
    """Run the greedy merge hierarchy once and record requested objectives."""

    requested_cells = sorted({int(value) for value in target_cells_values})
    if not requested_cells:
        return {}
    if requested_cells[0] < 1:
        raise ValueError("target_cells must be at least 1.")

    n = len(events.coords)
    minimum_cells = minimum_feasible_grid_lines(events) - 1
    if requested_cells[0] < minimum_cells:
        raise ValueError(
            f"Requested {requested_cells[0]} cells, but hard movement/topology "
            f"constraints require at least {minimum_cells} cells on this axis."
        )

    objectives: dict[int, float] = {
        cells: 0.0 for cells in requested_cells if cells + 1 >= n
    }
    requested_lines = {
        cells + 1 for cells in requested_cells if cells + 1 < n
    }
    if not requested_lines:
        return objectives

    model = _AxisCostModel(events)
    max_cluster_right = _max_cluster_right(events)

    left_index = np.arange(n, dtype=np.int32)
    right_index = np.arange(n, dtype=np.int32)
    raw_lower = events.coords - events.tol_minus
    raw_upper = events.coords + events.tol_plus
    cluster_cost = np.zeros(n, dtype=float)

    previous = np.arange(n, dtype=np.int32) - 1
    following = np.arange(n, dtype=np.int32) + 1
    following[-1] = -1

    active = np.ones(n, dtype=bool)
    version = np.zeros(n, dtype=np.int64)
    heap: list[tuple] = []

    def push_candidate(first: int, second: int) -> None:
        if (
            first < 0
            or second < 0
            or not active[first]
            or not active[second]
            or following[first] != second
        ):
            return

        merged_left = int(left_index[first])
        merged_right = int(right_index[second])
        if merged_right > int(max_cluster_right[merged_left]):
            return

        merged_lower = max(raw_lower[first], raw_lower[second])
        merged_upper = min(raw_upper[first], raw_upper[second])
        merged_cost, _ = model.segment_with_bounds(
            int(left_index[first]),
            int(right_index[second]),
            float(merged_lower),
            float(merged_upper),
        )
        if np.isfinite(merged_cost):
            increase = max(
                0.0,
                merged_cost - cluster_cost[first] - cluster_cost[second],
            )
            heapq.heappush(
                heap,
                (
                    increase,
                    first,
                    second,
                    int(version[first]),
                    int(version[second]),
                    merged_cost,
                    merged_lower,
                    merged_upper,
                ),
            )

    for index in range(n - 1):
        push_candidate(index, index + 1)

    cluster_count = n
    total_objective = 0.0
    if cluster_count in requested_lines:
        objectives[cluster_count - 1] = total_objective
    minimum_requested_lines = min(requested_lines)

    while cluster_count > minimum_requested_lines and heap:
        (
            _,
            first,
            second,
            version_first,
            version_second,
            merged_cost,
            merged_lower,
            merged_upper,
        ) = heapq.heappop(heap)

        if (
            not active[first]
            or not active[second]
            or following[first] != second
            or version[first] != version_first
            or version[second] != version_second
        ):
            continue

        old_cost = float(cluster_cost[first] + cluster_cost[second])
        total_objective += max(0.0, float(merged_cost - old_cost))

        right_index[first] = right_index[second]
        raw_lower[first] = merged_lower
        raw_upper[first] = merged_upper
        cluster_cost[first] = merged_cost

        after = following[second]
        following[first] = after
        if after >= 0:
            previous[after] = first

        active[second] = False
        version[first] += 1
        version[second] += 1
        cluster_count -= 1

        if cluster_count in requested_lines:
            objectives[cluster_count - 1] = float(total_objective)

        push_candidate(int(previous[first]), first)
        push_candidate(first, int(following[first]))

    missing = [cells for cells in requested_cells if cells not in objectives]
    if missing:
        raise RuntimeError(
            "The greedy merge hierarchy could not reach requested cell counts: "
            + ", ".join(map(str, missing))
        )
    return objectives


class _CoordinateMap:
    def __init__(
        self,
        original: np.ndarray,
        mapped: np.ndarray,
        tolerance: float,
    ):
        self.original = np.asarray(original, dtype=float)
        self.mapped = np.asarray(mapped, dtype=float)
        self.tolerance = float(tolerance)

    def __call__(self, value: float) -> float:
        value = float(value)
        insertion = int(np.searchsorted(self.original, value))
        candidates = []
        if insertion < len(self.original):
            candidates.append(insertion)
        if insertion > 0:
            candidates.append(insertion - 1)

        nearest = min(
            candidates,
            key=lambda index: abs(self.original[index] - value),
        )
        if abs(self.original[nearest] - value) > self.tolerance:
            raise ValueError(
                f"Coordinate {value} is absent from the quantizer; "
                f"nearest is {self.original[nearest]}."
            )
        return float(self.mapped[nearest])


def _clean_ring(points):
    cleaned: list[tuple[float, float]] = []
    for point in points:
        point = (float(point[0]), float(point[1]))
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)

    if cleaned and cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])

    if len(cleaned) < 4 or len(set(cleaned[:-1])) < 3:
        return None
    return cleaned


def _make_valid_compat(geometry):
    """Repair geometry across Shapely 1.8, 2.0, and 2.1+.

    Shapely 2.1+ accepts ``method`` and ``keep_collapsed``. Shapely 2.0
    exposes ``shapely.make_valid`` but accepts only the geometry. Older
    supported releases expose the function as ``shapely.validation.make_valid``.
    Only the specific unsupported-keyword error triggers the 2.0 fallback;
    unrelated TypeErrors are deliberately propagated.
    """

    if geometry.is_valid:
        return geometry

    make_valid = getattr(shapely, "make_valid", None)
    if make_valid is None:
        try:
            from shapely.validation import make_valid
        except ImportError as error:  # pragma: no cover - very old Shapely
            raise RuntimeError(
                "This module requires Shapely with make_valid support "
                "(Shapely 1.8+)."
            ) from error

    try:
        return make_valid(
            geometry,
            method="structure",
            keep_collapsed=False,
        )
    except TypeError as error:
        message = str(error)
        unsupported_option = (
            "unexpected keyword argument" in message
            and ("method" in message or "keep_collapsed" in message)
        )
        if not unsupported_option:
            raise
        return make_valid(geometry)


def snap_polygonal_geometry(geometry, map_x, map_y):
    """Apply the monotone axis mappings and repair collapsed components."""

    snapped_parts = []

    for polygon in _iter_polygons(geometry):
        shell = _clean_ring(
            [(map_x(x), map_y(y)) for x, y, *_ in polygon.exterior.coords]
        )
        if shell is None:
            continue

        holes = []
        for ring in polygon.interiors:
            hole = _clean_ring(
                [(map_x(x), map_y(y)) for x, y, *_ in ring.coords]
            )
            if hole is not None:
                holes.append(hole)

        candidate = Polygon(shell, holes)
        candidate = _make_valid_compat(candidate)
        candidate = _polygonal_only(candidate)
        if not candidate.is_empty:
            snapped_parts.append(candidate)

    return unary_union(snapped_parts) if snapped_parts else Polygon()



def _events_with_direction_locks(
    events: AxisEvents,
    lock_minus: np.ndarray,
    lock_plus: np.ndarray,
) -> AxisEvents:
    """Return an event set with selected movement directions frozen."""

    lock_minus = np.asarray(lock_minus, dtype=bool)
    lock_plus = np.asarray(lock_plus, dtype=bool)
    if lock_minus.shape != events.coords.shape or lock_plus.shape != events.coords.shape:
        raise ValueError("Direction lock arrays must match the event coordinates.")

    tol_minus = np.asarray(events.tol_minus, dtype=float).copy()
    tol_plus = np.asarray(events.tol_plus, dtype=float).copy()
    tol_minus[lock_minus] = 0.0
    tol_plus[lock_plus] = 0.0
    return AxisEvents(
        events.coords,
        events.w_minus,
        events.w_plus,
        tol_minus,
        tol_plus,
        events.max_cluster_right,
        events.protected_spans,
    )


def _scale_axis_events(events: AxisEvents, factor: float) -> AxisEvents:
    """Scale movement tolerances while preserving costs and topology."""

    factor = float(factor)
    if not np.isfinite(factor) or factor <= 0:
        raise ValueError("Axis-event tolerance scale factor must be positive.")
    return AxisEvents(
        events.coords,
        events.w_minus,
        events.w_plus,
        np.asarray(events.tol_minus, dtype=float) * factor,
        np.asarray(events.tol_plus, dtype=float) * factor,
        events.max_cluster_right,
        events.protected_spans,
    )


def _result_with_minimum_line_spacing(
    events: AxisEvents,
    result: tuple[np.ndarray, np.ndarray, float, np.ndarray],
    minimum_width: float | None,
    tolerance: float,
) -> tuple[np.ndarray, np.ndarray, float, np.ndarray] | None:
    """Adjust representatives for fixed clusters to satisfy minimum spacing.

    The partition itself is left unchanged. Representatives are moved only
    inside the hard movement interval already permitted for their cluster.
    Returns ``None`` when those fixed clusters cannot satisfy the spacing.
    """

    lines, mapping, objective, cuts = result
    lines = np.asarray(lines, dtype=float)
    cuts = np.asarray(cuts, dtype=np.int32)
    if minimum_width is None or minimum_width <= 0.0:
        return lines, np.asarray(mapping, dtype=float), float(objective), cuts
    minimum_width = float(minimum_width)
    if np.all(np.diff(lines) + tolerance >= minimum_width):
        return lines, np.asarray(mapping, dtype=float), float(objective), cuts

    cluster_count = len(lines)
    if cluster_count < 2:
        return None
    span = float(events.coords[-1] - events.coords[0])
    if minimum_width * (cluster_count - 1) > span + tolerance:
        return None

    lower = np.empty(cluster_count, dtype=float)
    upper = np.empty(cluster_count, dtype=float)
    for cluster in range(cluster_count):
        left = int(cuts[cluster])
        right = int(cuts[cluster + 1] - 1)
        if left < 0 or right < left or right >= len(events.coords):
            return None
        raw_lower = float(
            np.max(
                events.coords[left : right + 1]
                - events.tol_minus[left : right + 1]
            )
        )
        raw_upper = float(
            np.min(
                events.coords[left : right + 1]
                + events.tol_plus[left : right + 1]
            )
        )
        # Under the spacing constraint a whole cluster may move outside its
        # original coordinate hull, provided every member's movement tolerance
        # permits it and the sequence of representatives remains ordered.
        lower[cluster] = raw_lower
        upper[cluster] = raw_upper
        if lower[cluster] > upper[cluster] + tolerance:
            return None

    # Earliest/latest feasible positions under q[j+1] - q[j] >= minimum_width.
    earliest = lower.copy()
    for cluster in range(1, cluster_count):
        earliest[cluster] = max(
            earliest[cluster],
            earliest[cluster - 1] + minimum_width,
        )

    latest = upper.copy()
    for cluster in range(cluster_count - 2, -1, -1):
        latest[cluster] = min(
            latest[cluster],
            latest[cluster + 1] - minimum_width,
        )

    if np.any(earliest > latest + tolerance):
        return None

    # Stay as close as possible to the unconstrained representatives while
    # maintaining a feasible forward chain. This is deterministic and keeps
    # the original surrogate optimum whenever it already satisfies spacing.
    representatives = np.empty(cluster_count, dtype=float)
    representatives[0] = float(np.clip(lines[0], earliest[0], latest[0]))
    for cluster in range(1, cluster_count):
        required = representatives[cluster - 1] + minimum_width
        low = max(earliest[cluster], required)
        high = latest[cluster]
        if low > high + tolerance:
            return None
        representatives[cluster] = float(np.clip(lines[cluster], low, high))

    if np.any(np.diff(representatives) + tolerance < minimum_width):
        return None

    constrained_mapping = np.empty(len(events.coords), dtype=float)
    constrained_objective = 0.0
    for cluster in range(cluster_count):
        left = int(cuts[cluster])
        right = int(cuts[cluster + 1] - 1)
        representative = float(representatives[cluster])
        coordinates = events.coords[left : right + 1]
        w_minus = events.w_minus[left : right + 1]
        w_plus = events.w_plus[left : right + 1]
        movement = representative - coordinates
        cost = float(
            np.sum(
                np.where(
                    movement < 0.0,
                    (-movement) * w_minus,
                    movement * w_plus,
                )
            )
        )
        if not np.isfinite(cost):
            return None
        constrained_mapping[left : right + 1] = representative
        constrained_objective += cost

    return (
        representatives,
        constrained_mapping,
        float(constrained_objective),
        cuts.copy(),
    )


def _snap_records_with_mappings(
    records,
    x_events: AxisEvents,
    y_events: AxisEvents,
    x_mapping: np.ndarray,
    y_mapping: np.ndarray,
    coord_eps: float,
):
    map_x = _CoordinateMap(
        x_events.coords,
        x_mapping,
        tolerance=max(coord_eps * 2.0, 1e-10),
    )
    map_y = _CoordinateMap(
        y_events.coords,
        y_mapping,
        tolerance=max(coord_eps * 2.0, 1e-10),
    )

    snapped_records = []
    for source_index, record in enumerate(records):
        geometry = record["geometry"]
        if geometry is None or geometry.is_empty:
            continue

        snapped = snap_polygonal_geometry(geometry, map_x, map_y)
        if snapped.is_empty:
            continue

        snapped_records.append(
            {
                "geometry": snapped,
                "load": float(record["load"]),
                "source_index": source_index,
            }
        )
    return snapped_records


def _allowed_load_unions(records) -> list[Any]:
    """Original area where each source load may safely exist after snapping.

    The effective background load is zero.  For a positive source load L,
    snapping may only place that source inside territory whose original load
    was already >= L.  This is stronger than checking only explicit lower-load
    polygons: uncovered gaps are also protected from positive-load expansion.

    A zero-load source may expand anywhere inside the immutable field extent,
    because doing so does not raise the background load above zero.
    """

    groups: dict[float, list[Any]] = defaultdict(list)
    non_empty_geometries: list[Any] = []
    for record in records:
        geometry = record["geometry"]
        if geometry is None or geometry.is_empty:
            continue
        load = float(record["load"])
        groups[load].append(geometry)
        non_empty_geometries.append(geometry)

    if not non_empty_geometries:
        return [Polygon() for _ in records]

    bounds = np.asarray([geometry.bounds for geometry in non_empty_geometries], dtype=float)
    min_x, min_y = np.min(bounds[:, 0]), np.min(bounds[:, 1])
    max_x, max_y = np.max(bounds[:, 2]), np.max(bounds[:, 3])
    field_extent = Polygon(
        [
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (min_x, max_y),
        ]
    )

    allowed_by_load: dict[float, Any] = {}
    accumulated = Polygon()
    for load in sorted(groups, reverse=True):
        current = unary_union(groups[load])
        accumulated = current if accumulated.is_empty else unary_union([accumulated, current])
        allowed_by_load[load] = accumulated

    result: list[Any] = []
    for record in records:
        geometry = record["geometry"]
        if geometry is None or geometry.is_empty:
            result.append(Polygon())
            continue
        load = float(record["load"])
        if load <= 0.0:
            result.append(field_extent)
        else:
            result.append(allowed_by_load.get(load, Polygon()))
    return result


def _load_order_violations(
    records,
    snapped_records,
    allowed_unions,
    area_tolerance: float,
) -> list[tuple[int, float]]:
    """Find snapped source area whose original effective load was smaller."""

    snapped_by_source = {
        int(record["source_index"]): record["geometry"]
        for record in snapped_records
    }
    violations: list[tuple[int, float]] = []

    for source_index, record in enumerate(records):
        original = record["geometry"]
        allowed = allowed_unions[source_index]
        if original is None or original.is_empty:
            continue

        snapped = snapped_by_source.get(source_index, Polygon())
        if snapped.is_empty:
            continue

        forbidden = snapped.difference(allowed)
        forbidden_area = float(forbidden.area)
        if forbidden_area > area_tolerance:
            violations.append((source_index, forbidden_area))

    return violations


def _event_index(coords: np.ndarray, value: float, tolerance: float) -> int:
    index = int(np.searchsorted(coords, value))
    candidates = []
    if index < len(coords):
        candidates.append(index)
    if index > 0:
        candidates.append(index - 1)
    if not candidates:
        raise RuntimeError("No axis coordinates are available for load protection.")
    nearest = min(candidates, key=lambda candidate: abs(coords[candidate] - value))
    if abs(coords[nearest] - value) > tolerance:
        raise RuntimeError(
            f"Coordinate {value} is absent while applying load-order protection."
        )
    return int(nearest)


def _lock_outward_moves_for_record(
    geometry,
    x_events: AxisEvents,
    y_events: AxisEvents,
    x_mapping: np.ndarray,
    y_mapping: np.ndarray,
    lock_x_minus: np.ndarray,
    lock_x_plus: np.ndarray,
    lock_y_minus: np.ndarray,
    lock_y_plus: np.ndarray,
    coord_eps: float,
) -> int:
    """Freeze outward directions that participated in a forbidden gain."""

    tolerance = max(coord_eps * 2.0, 1e-10)
    newly_locked = 0

    def set_lock(store: np.ndarray, index: int) -> None:
        nonlocal newly_locked
        if not store[index]:
            store[index] = True
            newly_locked += 1

    for polygon_0 in _iter_polygons(geometry):
        polygon = orient(polygon_0, sign=1.0)
        for ring in [polygon.exterior, *polygon.interiors]:
            coordinates = list(ring.coords)
            for first, second in zip(coordinates[:-1], coordinates[1:]):
                x1 = _canonical(first[0], coord_eps)
                y1 = _canonical(first[1], coord_eps)
                x2 = _canonical(second[0], coord_eps)
                y2 = _canonical(second[1], coord_eps)
                dx, dy = x2 - x1, y2 - y1

                if abs(dx) <= coord_eps and abs(dy) > coord_eps:
                    coordinate = _canonical((x1 + x2) / 2.0, coord_eps)
                    index = _event_index(x_events.coords, coordinate, tolerance)
                    movement = float(x_mapping[index] - coordinate)
                    interior_is_positive = dy < 0
                    if interior_is_positive and movement < -tolerance:
                        set_lock(lock_x_minus, index)
                    elif not interior_is_positive and movement > tolerance:
                        set_lock(lock_x_plus, index)
                elif abs(dy) <= coord_eps and abs(dx) > coord_eps:
                    coordinate = _canonical((y1 + y2) / 2.0, coord_eps)
                    index = _event_index(y_events.coords, coordinate, tolerance)
                    movement = float(y_mapping[index] - coordinate)
                    interior_is_positive = dx > 0
                    if interior_is_positive and movement < -tolerance:
                        set_lock(lock_y_minus, index)
                    elif not interior_is_positive and movement > tolerance:
                        set_lock(lock_y_plus, index)

    return newly_locked


def quantize_rectilinear_loads(
    records: Iterable[Mapping[str, Any]],
    target_cells_x: int | None = None,
    target_cells_y: int | None = None,
    *,
    max_cells_x: int | None = None,
    max_cells_y: int | None = None,
    max_total_cells: int | None = None,
    min_cell_width_x: float | None = None,
    min_cell_width_y: float | None = None,
    method: str = "auto",
    exact_coordinate_limit: int = 2000,
    exact_work_limit: int = 120_000_000,
    coord_eps: float = 1e-8,
    max_shift_fraction: float = 0.02,
    max_auto_shift_fraction: float | None = None,
    auto_shift_search_steps: int = 24,
    shrink_penalty: float = 20.0,
    expand_penalty: float = 1.0,
    load_gamma: float = 2.0,
    base_priority: float = 0.03,
    min_shrink_tol_ratio: float = 0.10,
    min_expand_tol_ratio: float = 0.50,
    load_scale: float | None = None,
    merge_equal_loads: bool = True,
    preserve_holes: bool = True,
    min_hole_area: float = 0.0,
    min_hole_area_fraction: float = 0.0,
    protect_load_order: bool = False,
    load_overlap_tolerance: float = 1e-12,
    max_load_guard_iterations: int = 64,
):
    """
    Quantize rectilinear load polygons onto a reduced rectilinear grid.

    ``method``:
      - ``"exact"``: exact DP for the surrogate objective;
      - ``"greedy"``: scalable adjacent merging;
      - ``"auto"``: exact only when the estimated work is acceptable.

    Without a cell budget, passing ``None`` for an axis preserves the legacy
    behavior and selects the minimum feasible number of cells on that axis.

    With ``max_cells_x``, ``max_cells_y``, or ``max_total_cells``, every
    ``None`` target becomes an optimization variable. The chosen pair
    minimizes ``objective_x + objective_y`` subject to all supplied limits.

    ``min_cell_width_x`` and ``min_cell_width_y`` are hard lower bounds on
    every resulting cell width along the corresponding axis. Existing grid
    lines are first moved inside their permitted tolerances to satisfy the
    bound; candidate grids are rejected only when that is impossible.

    ``max_auto_shift_fraction`` is an optional fallback ceiling. It is used
    only when the requested budget cannot be met at ``max_shift_fraction``.

    When ``protect_load_order`` is true, the returned result is validated so
    that no source occupies territory whose original effective load was
    smaller. Uncovered background is treated as load 0, so positive loads may
    not expand into gaps between the supplied polygons. Violating outward
    directions are frozen and the optimization is repeated until the hard
    condition is satisfied.
    """

    records = list(records)
    if load_overlap_tolerance < 0:
        raise ValueError("load_overlap_tolerance must be non-negative.")
    max_load_guard_iterations = int(max_load_guard_iterations)
    if max_load_guard_iterations < 1:
        raise ValueError("max_load_guard_iterations must be at least 1.")
    auto_shift_search_steps = int(auto_shift_search_steps)
    if auto_shift_search_steps < 1:
        raise ValueError("auto_shift_search_steps must be at least 1.")

    def positive_limit(value: int | None, name: str) -> int | None:
        if value is None:
            return None
        value = int(value)
        if value < 1:
            raise ValueError(f"{name} must be at least 1.")
        return value

    max_cells_x = positive_limit(max_cells_x, "max_cells_x")
    max_cells_y = positive_limit(max_cells_y, "max_cells_y")
    max_total_cells = positive_limit(max_total_cells, "max_total_cells")

    def minimum_width(value: float | None, name: str) -> float | None:
        if value is None:
            return None
        value = float(value)
        if not np.isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be a finite non-negative number or None.")
        return value

    min_cell_width_x = minimum_width(min_cell_width_x, "min_cell_width_x")
    min_cell_width_y = minimum_width(min_cell_width_y, "min_cell_width_y")
    width_tolerance = max(coord_eps * 2.0, 1e-10)

    def axis_width_ok(lines: np.ndarray, minimum: float | None) -> bool:
        if minimum is None or minimum <= 0.0:
            return True
        lines = np.asarray(lines, dtype=float)
        return bool(np.all(np.diff(lines) + width_tolerance >= minimum))

    def actual_min_width(lines: np.ndarray) -> float:
        widths = np.diff(np.asarray(lines, dtype=float))
        return float(np.min(widths)) if widths.size else math.inf

    def width_cell_capacity(events: AxisEvents, minimum: float | None) -> int:
        original_cells = len(events.coords) - 1
        if minimum is None or minimum <= 0.0:
            return original_cells
        span = float(events.coords[-1] - events.coords[0])
        if span + width_tolerance < minimum:
            return 0
        return max(1, min(original_cells, int(math.floor((span + width_tolerance) / minimum))))

    budget_requested = any(
        value is not None
        for value in (max_cells_x, max_cells_y, max_total_cells)
    )

    max_shift_fraction = float(max_shift_fraction)
    if max_auto_shift_fraction is not None:
        max_auto_shift_fraction = float(max_auto_shift_fraction)
        if max_auto_shift_fraction < max_shift_fraction:
            raise ValueError(
                "max_auto_shift_fraction must be greater than or equal to "
                "max_shift_fraction."
            )

    reference_x_events, reference_y_events = build_axis_events(
        records,
        coord_eps=coord_eps,
        max_shift_fraction=max_shift_fraction,
        shrink_penalty=shrink_penalty,
        expand_penalty=expand_penalty,
        load_gamma=load_gamma,
        base_priority=base_priority,
        min_shrink_tol_ratio=min_shrink_tol_ratio,
        min_expand_tol_ratio=min_expand_tol_ratio,
        load_scale=load_scale,
        merge_equal_loads=merge_equal_loads,
        preserve_holes=preserve_holes,
        min_hole_area=min_hole_area,
        min_hole_area_fraction=min_hole_area_fraction,
    )

    def build_events_for_shift(shift_fraction: float):
        factor = float(shift_fraction) / max_shift_fraction
        if abs(factor - 1.0) <= 1e-15:
            return reference_x_events, reference_y_events
        return (
            _scale_axis_events(reference_x_events, factor),
            _scale_axis_events(reference_y_events, factor),
        )

    def solve_axis(events: AxisEvents, target_cells: int):
        selected_method = method
        target_cells = int(target_cells)
        target_lines = target_cells + 1
        estimated_work = target_lines * len(events.coords) ** 2

        if selected_method == "auto":
            selected_method = (
                "exact"
                if (
                    len(events.coords) <= exact_coordinate_limit
                    and estimated_work <= exact_work_limit
                )
                else "greedy"
            )

        if selected_method == "exact":
            return quantize_axis_exact(events, target_cells), selected_method
        if selected_method == "greedy":
            return quantize_axis_greedy(events, target_cells), selected_method
        raise ValueError("method must be 'auto', 'exact', or 'greedy'.")

    class _InfeasibleGrid(Exception):
        pass

    def select_grid(current_x_events: AxisEvents, current_y_events: AxisEvents):
        minimum_cells_x = minimum_feasible_grid_lines(current_x_events) - 1
        minimum_cells_y = minimum_feasible_grid_lines(current_y_events) - 1

        if not budget_requested:
            selected_x = (
                minimum_cells_x if target_cells_x is None else int(target_cells_x)
            )
            selected_y = (
                minimum_cells_y if target_cells_y is None else int(target_cells_y)
            )
            if selected_x < minimum_cells_x or selected_y < minimum_cells_y:
                raise _InfeasibleGrid(
                    "Requested targets are below the minimum feasible grid size."
                )
            if selected_x > width_cell_capacity(current_x_events, min_cell_width_x):
                raise _InfeasibleGrid(
                    "Requested X target cannot satisfy the minimum cell width."
                )
            if selected_y > width_cell_capacity(current_y_events, min_cell_width_y):
                raise _InfeasibleGrid(
                    "Requested Y target cannot satisfy the minimum cell width."
                )
            try:
                x_result, method_x = solve_axis(current_x_events, selected_x)
                y_result, method_y = solve_axis(current_y_events, selected_y)
            except ValueError as error:
                raise _InfeasibleGrid(str(error)) from error

            x_result = _result_with_minimum_line_spacing(
                current_x_events,
                x_result,
                min_cell_width_x,
                width_tolerance,
            )
            y_result = _result_with_minimum_line_spacing(
                current_y_events,
                y_result,
                min_cell_width_y,
                width_tolerance,
            )
            if x_result is None:
                raise _InfeasibleGrid(
                    "Requested X grid cannot satisfy the minimum cell width."
                )
            if y_result is None:
                raise _InfeasibleGrid(
                    "Requested Y grid cannot satisfy the minimum cell width."
                )
        else:
            original_cells_x = len(current_x_events.coords) - 1
            original_cells_y = len(current_y_events.coords) - 1

            def axis_candidates(
                requested: int | None,
                minimum: int,
                original: int,
                axis_maximum: int | None,
                width_capacity: int,
            ) -> list[int]:
                if requested is not None:
                    requested = int(requested)
                    if requested < minimum or requested < 1:
                        return []
                    if requested > width_capacity:
                        return []
                    if axis_maximum is not None and requested > axis_maximum:
                        return []
                    if max_total_cells is not None and requested > max_total_cells:
                        return []
                    return [requested]

                upper = min(original, width_capacity)
                if axis_maximum is not None:
                    upper = min(upper, axis_maximum)
                if max_total_cells is not None:
                    upper = min(upper, max_total_cells)
                return list(range(minimum, upper + 1)) if minimum <= upper else []

            x_candidates = axis_candidates(
                target_cells_x,
                minimum_cells_x,
                original_cells_x,
                max_cells_x,
                width_cell_capacity(current_x_events, min_cell_width_x),
            )
            y_candidates = axis_candidates(
                target_cells_y,
                minimum_cells_y,
                original_cells_y,
                max_cells_y,
                width_cell_capacity(current_y_events, min_cell_width_y),
            )
            if not x_candidates or not y_candidates:
                if (
                    (min_cell_width_x is not None and min_cell_width_x > 0.0)
                    or (min_cell_width_y is not None and min_cell_width_y > 0.0)
                ):
                    raise _InfeasibleGrid(
                        "Requested cell budget/targets cannot satisfy the minimum cell width."
                    )
                raise _InfeasibleGrid(
                    "Requested cell budget is below the minimum feasible grid size."
                )

            if max_total_cells is not None:
                smallest_x = min(x_candidates)
                smallest_y = min(y_candidates)
                x_candidates = [
                    cells
                    for cells in x_candidates
                    if cells <= max_total_cells // smallest_y
                ]
                y_candidates = [
                    cells
                    for cells in y_candidates
                    if cells <= max_total_cells // smallest_x
                ]
                if not x_candidates or not y_candidates:
                    raise _InfeasibleGrid(
                        "Requested total-cell budget is below the minimum "
                        "feasible X/Y product."
                    )

            def axis_frontier(events: AxisEvents, candidates: list[int]):
                selected_method = method
                largest_target_lines = max(candidates) + 1
                estimated_work = largest_target_lines * len(events.coords) ** 2
                if selected_method == "auto":
                    selected_method = (
                        "exact"
                        if (
                            len(events.coords) <= exact_coordinate_limit
                            and estimated_work <= exact_work_limit
                        )
                        else "greedy"
                    )

                cached: dict[
                    int,
                    tuple[np.ndarray, np.ndarray, float, np.ndarray],
                ] = {}

                if selected_method == "exact":
                    n = len(events.coords)
                    max_clusters = min(largest_target_lines, n)
                    costs = _precompute_segment_costs(
                        events.coords,
                        events.w_minus,
                        events.w_plus,
                        events.tol_minus,
                        events.tol_plus,
                        _max_cluster_right(events),
                    )
                    objectives, back = _exact_partition_frontier(
                        costs,
                        max_clusters,
                    )
                    objective_by_cells = {
                        cells: (
                            0.0
                            if cells + 1 >= n
                            else float(objectives[cells + 1])
                        )
                        for cells in candidates
                    }

                    def materialize(cells: int):
                        if cells not in cached:
                            cached[cells] = _exact_result_from_frontier(
                                events,
                                cells,
                                objectives,
                                back,
                            )
                        return cached[cells]

                    return objective_by_cells, materialize, selected_method

                if selected_method == "greedy":
                    objective_by_cells = _greedy_objective_frontier(
                        events,
                        candidates,
                    )

                    def materialize(cells: int):
                        if cells not in cached:
                            cached[cells] = quantize_axis_greedy(events, cells)
                        return cached[cells]

                    return objective_by_cells, materialize, selected_method

                raise ValueError("method must be 'auto', 'exact', or 'greedy'.")

            x_objectives, materialize_x, method_x = axis_frontier(
                current_x_events,
                x_candidates,
            )
            y_objectives, materialize_y, method_y = axis_frontier(
                current_y_events,
                y_candidates,
            )

            def constrain_frontier_width(
                events: AxisEvents,
                candidates: list[int],
                objective_by_cells: dict[int, float],
                materialize,
                minimum_width: float | None,
            ):
                if minimum_width is None or minimum_width <= 0.0:
                    return candidates, objective_by_cells, materialize

                constrained: dict[
                    int,
                    tuple[np.ndarray, np.ndarray, float, np.ndarray],
                ] = {}
                feasible: list[int] = []
                for cells in candidates:
                    adjusted = _result_with_minimum_line_spacing(
                        events,
                        materialize(cells),
                        minimum_width,
                        width_tolerance,
                    )
                    if adjusted is None:
                        continue
                    feasible.append(cells)
                    constrained[cells] = adjusted
                    objective_by_cells[cells] = float(adjusted[2])

                original_materialize = materialize

                def constrained_materialize(cells: int):
                    if cells in constrained:
                        return constrained[cells]
                    return original_materialize(cells)

                return feasible, objective_by_cells, constrained_materialize

            x_candidates, x_objectives, materialize_x = constrain_frontier_width(
                current_x_events,
                x_candidates,
                x_objectives,
                materialize_x,
                min_cell_width_x,
            )
            y_candidates, y_objectives, materialize_y = constrain_frontier_width(
                current_y_events,
                y_candidates,
                y_objectives,
                materialize_y,
                min_cell_width_y,
            )
            if not x_candidates or not y_candidates:
                raise _InfeasibleGrid(
                    "No quantized grid satisfies the minimum cell width."
                )

            feasible_pairs = []
            for cells_x in x_candidates:
                for cells_y in y_candidates:
                    if (
                        max_total_cells is not None
                        and cells_x * cells_y > max_total_cells
                    ):
                        continue
                    objective = float(
                        x_objectives[cells_x] + y_objectives[cells_y]
                    )
                    feasible_pairs.append(
                        (
                            objective,
                            cells_x * cells_y,
                            abs(cells_x - cells_y),
                            cells_x,
                            cells_y,
                        )
                    )

            if not feasible_pairs:
                raise _InfeasibleGrid(
                    "Requested cell budget has no feasible X/Y allocation."
                )

            (
                _,
                _,
                _,
                selected_x,
                selected_y,
            ) = min(feasible_pairs, key=lambda item: item[:5])
            x_result = materialize_x(selected_x)
            y_result = materialize_y(selected_y)

        x_lines, x_mapping, objective_x, x_cuts = x_result
        y_lines, y_mapping, objective_y, y_cuts = y_result
        return {
            "x_lines": x_lines,
            "y_lines": y_lines,
            "x_mapping": x_mapping,
            "y_mapping": y_mapping,
            "objective_x": objective_x,
            "objective_y": objective_y,
            "x_cuts": x_cuts,
            "y_cuts": y_cuts,
            "method_x": method_x,
            "method_y": method_y,
            "minimum_cells_x": minimum_cells_x,
            "minimum_cells_y": minimum_cells_y,
            "selected_cells_x": len(x_lines) - 1,
            "selected_cells_y": len(y_lines) - 1,
        }

    def allocation_is_feasible(
        current_x_events: AxisEvents,
        current_y_events: AxisEvents,
    ) -> bool:
        """Cheap feasibility check used by automatic shift relaxation."""

        minimum_x = minimum_feasible_grid_lines(current_x_events) - 1
        minimum_y = minimum_feasible_grid_lines(current_y_events) - 1

        def smallest_candidate(
            requested: int | None,
            minimum: int,
            original: int,
            axis_maximum: int | None,
            width_capacity: int,
        ) -> int | None:
            if requested is not None:
                candidate = int(requested)
                if candidate < minimum or candidate < 1:
                    return None
                if candidate > width_capacity:
                    return None
                if axis_maximum is not None and candidate > axis_maximum:
                    return None
                if max_total_cells is not None and candidate > max_total_cells:
                    return None
                return candidate

            if not budget_requested:
                return minimum if minimum <= width_capacity else None

            upper = min(original, width_capacity)
            if axis_maximum is not None:
                upper = min(upper, axis_maximum)
            if max_total_cells is not None:
                upper = min(upper, max_total_cells)
            return minimum if minimum <= upper else None

        candidate_x = smallest_candidate(
            target_cells_x,
            minimum_x,
            len(current_x_events.coords) - 1,
            max_cells_x,
            width_cell_capacity(current_x_events, min_cell_width_x),
        )
        candidate_y = smallest_candidate(
            target_cells_y,
            minimum_y,
            len(current_y_events.coords) - 1,
            max_cells_y,
            width_cell_capacity(current_y_events, min_cell_width_y),
        )
        if candidate_x is None or candidate_y is None:
            return False
        return (
            max_total_cells is None
            or candidate_x * candidate_y <= max_total_cells
        )

    used_max_shift_fraction = max_shift_fraction
    raw_x_events, raw_y_events = build_events_for_shift(used_max_shift_fraction)
    lock_x_minus = np.zeros(len(raw_x_events.coords), dtype=bool)
    lock_x_plus = np.zeros(len(raw_x_events.coords), dtype=bool)
    lock_y_minus = np.zeros(len(raw_y_events.coords), dtype=bool)
    lock_y_plus = np.zeros(len(raw_y_events.coords), dtype=bool)

    allowed_load_unions = _allowed_load_unions(records) if protect_load_order else None
    load_guard_iterations = 0
    load_guard_blocked_directions = 0
    maximum_forbidden_overlap_area = 0.0

    while True:
        current_x_events = _events_with_direction_locks(
            raw_x_events,
            lock_x_minus,
            lock_x_plus,
        )
        current_y_events = _events_with_direction_locks(
            raw_y_events,
            lock_y_minus,
            lock_y_plus,
        )

        if not allocation_is_feasible(current_x_events, current_y_events):
            can_relax = (
                max_auto_shift_fraction is not None
                and max_auto_shift_fraction > used_max_shift_fraction
            )
            if not can_relax:
                width_note = (
                    ", and minimum cell width"
                    if (
                        (min_cell_width_x is not None and min_cell_width_x > 0.0)
                        or (min_cell_width_y is not None and min_cell_width_y > 0.0)
                    )
                    else ""
                )
                raise ValueError(
                    "Requested cell budget/targets are infeasible under the "
                    f"current movement, topology, load-order{width_note} constraints."
                )

            high_x_events, high_y_events = build_events_for_shift(
                max_auto_shift_fraction
            )
            if (
                not np.array_equal(high_x_events.coords, raw_x_events.coords)
                or not np.array_equal(high_y_events.coords, raw_y_events.coords)
            ):
                raise RuntimeError(
                    "Axis coordinates changed during automatic shift relaxation."
                )
            locked_high_x = _events_with_direction_locks(
                high_x_events,
                lock_x_minus,
                lock_x_plus,
            )
            locked_high_y = _events_with_direction_locks(
                high_y_events,
                lock_y_minus,
                lock_y_plus,
            )
            if not allocation_is_feasible(locked_high_x, locked_high_y):
                raise ValueError(
                    "Requested cell budget/targets remain infeasible even at "
                    "max_auto_shift_fraction."
                )

            low = used_max_shift_fraction
            high = float(max_auto_shift_fraction)
            best_x_events = high_x_events
            best_y_events = high_y_events
            for _ in range(auto_shift_search_steps):
                midpoint = (low + high) / 2.0
                mid_x_events, mid_y_events = build_events_for_shift(midpoint)
                locked_mid_x = _events_with_direction_locks(
                    mid_x_events,
                    lock_x_minus,
                    lock_x_plus,
                )
                locked_mid_y = _events_with_direction_locks(
                    mid_y_events,
                    lock_y_minus,
                    lock_y_plus,
                )
                if allocation_is_feasible(locked_mid_x, locked_mid_y):
                    high = midpoint
                    best_x_events = mid_x_events
                    best_y_events = mid_y_events
                else:
                    low = midpoint

            used_max_shift_fraction = high
            raw_x_events, raw_y_events = best_x_events, best_y_events
            continue

        try:
            selected = select_grid(current_x_events, current_y_events)
        except _InfeasibleGrid as error:
            can_relax = (
                max_auto_shift_fraction is not None
                and max_auto_shift_fraction > used_max_shift_fraction
            )
            if can_relax:
                relaxed_x_events, relaxed_y_events = build_events_for_shift(
                    max_auto_shift_fraction
                )
                if (
                    not np.array_equal(relaxed_x_events.coords, raw_x_events.coords)
                    or not np.array_equal(relaxed_y_events.coords, raw_y_events.coords)
                ):
                    raise RuntimeError(
                        "Axis coordinates changed during automatic shift relaxation."
                    )
                raw_x_events, raw_y_events = relaxed_x_events, relaxed_y_events
                used_max_shift_fraction = max_auto_shift_fraction
                continue
            raise ValueError(
                "Requested cell budget/targets are infeasible under the current "
                "movement, topology, and load-order constraints. "
                f"Details: {error}"
            ) from error

        snapped_records = _snap_records_with_mappings(
            records,
            current_x_events,
            current_y_events,
            selected["x_mapping"],
            selected["y_mapping"],
            coord_eps,
        )

        if not protect_load_order:
            violations = []
            break

        violations = _load_order_violations(
            records,
            snapped_records,
            allowed_load_unions,
            float(load_overlap_tolerance),
        )
        if not violations:
            break

        maximum_forbidden_overlap_area = max(
            maximum_forbidden_overlap_area,
            max(area for _, area in violations),
        )
        if load_guard_iterations >= max_load_guard_iterations:
            raise RuntimeError(
                "Load-order protection did not converge within "
                f"{max_load_guard_iterations} iterations."
            )

        newly_locked = 0
        for source_index, _ in violations:
            newly_locked += _lock_outward_moves_for_record(
                records[source_index]["geometry"],
                current_x_events,
                current_y_events,
                selected["x_mapping"],
                selected["y_mapping"],
                lock_x_minus,
                lock_x_plus,
                lock_y_minus,
                lock_y_plus,
                coord_eps,
            )

        if newly_locked == 0:
            raise RuntimeError(
                "A forbidden high-load overlap was found, but no outward axis "
                "movement could be isolated for another optimization pass."
            )

        load_guard_iterations += 1
        load_guard_blocked_directions += newly_locked

    x_lines = selected["x_lines"]
    y_lines = selected["y_lines"]
    x_mapping = selected["x_mapping"]
    y_mapping = selected["y_mapping"]
    objective_x = selected["objective_x"]
    objective_y = selected["objective_y"]
    x_cuts = selected["x_cuts"]
    y_cuts = selected["y_cuts"]

    return {
        "x_lines": x_lines,
        "y_lines": y_lines,
        "x_original": current_x_events.coords,
        "y_original": current_y_events.coords,
        "x_mapping": x_mapping,
        "y_mapping": y_mapping,
        "snapped": snapped_records,
        "objective_x": objective_x,
        "objective_y": objective_y,
        "objective_total": float(objective_x + objective_y),
        "method_x": selected["method_x"],
        "method_y": selected["method_y"],
        "x_cuts": x_cuts,
        "y_cuts": y_cuts,
        "minimum_cells_x": selected["minimum_cells_x"],
        "minimum_cells_y": selected["minimum_cells_y"],
        "selected_cells_x": len(x_lines) - 1,
        "selected_cells_y": len(y_lines) - 1,
        "total_cells": (len(x_lines) - 1) * (len(y_lines) - 1),
        "requested_target_cells_x": target_cells_x,
        "requested_target_cells_y": target_cells_y,
        "max_cells_x": max_cells_x,
        "max_cells_y": max_cells_y,
        "max_total_cells": max_total_cells,
        "min_cell_width_x": min_cell_width_x,
        "min_cell_width_y": min_cell_width_y,
        "actual_min_cell_width_x": actual_min_width(x_lines),
        "actual_min_cell_width_y": actual_min_width(y_lines),
        "used_max_shift_fraction": used_max_shift_fraction,
        "auto_shift_relaxed": (
            used_max_shift_fraction > max_shift_fraction + 1e-15
        ),
        "load_order_guard_enabled": bool(protect_load_order),
        "load_order_guard_passed": not violations,
        "load_order_guard_iterations": load_guard_iterations,
        "load_order_guard_blocked_directions": load_guard_blocked_directions,
        "maximum_forbidden_overlap_area_seen": maximum_forbidden_overlap_area,
        "protected_hole_spans_x": (
            0
            if current_x_events.protected_spans is None
            else len(current_x_events.protected_spans)
        ),
        "protected_hole_spans_y": (
            0
            if current_y_events.protected_spans is None
            else len(current_y_events.protected_spans)
        ),
    }

def build_load_matrix(
    snapped_records,
    x_lines: np.ndarray,
    y_lines: np.ndarray,
    *,
    background: float = np.nan,
    chunk_rows: int = 512,
) -> np.ndarray:
    """
    Fill the reduced matrix by testing cell centres.

    Rows follow increasing Y. Use ``np.flipud(matrix)`` when row zero
    should represent the top of the field.
    """

    geometries = [record["geometry"] for record in snapped_records]
    loads = np.asarray(
        [float(record["load"]) for record in snapped_records],
        dtype=float,
    )

    row_count = len(y_lines) - 1
    column_count = len(x_lines) - 1
    matrix = np.full((row_count, column_count), -np.inf, dtype=float)

    if not geometries:
        matrix.fill(background)
        return matrix

    tree = STRtree(geometries)
    x_centres = (x_lines[:-1] + x_lines[1:]) / 2.0
    y_centres = (y_lines[:-1] + y_lines[1:]) / 2.0

    for first_row in range(0, row_count, chunk_rows):
        last_row = min(row_count, first_row + chunk_rows)
        xx, yy = np.meshgrid(x_centres, y_centres[first_row:last_row])
        points = shapely.points(xx.ravel(), yy.ravel())

        pairs = tree.query(points, predicate="within")
        if pairs.size:
            point_indices = pairs[0]
            geometry_indices = pairs[1]
            block = matrix[first_row:last_row].reshape(-1)

            # Normally there is one polygon per point. max is conservative
            # if validation/repair created an accidental overlap.
            np.maximum.at(
                block,
                point_indices,
                loads[geometry_indices],
            )

    matrix[~np.isfinite(matrix)] = background
    return matrix


def distortion_report(original_records, snapped_records):
    """Per-source geometric loss/gain report."""

    original_records = list(original_records)
    snapped_by_source = {
        int(record["source_index"]): record["geometry"]
        for record in snapped_records
    }

    rows = []
    for source_index, record in enumerate(original_records):
        original = record["geometry"]
        snapped = snapped_by_source.get(source_index, Polygon())

        original_area = float(original.area)
        snapped_area = float(snapped.area)
        lost_area = (
            float(original.difference(snapped).area)
            if not original.is_empty
            else 0.0
        )
        gained_area = (
            float(snapped.difference(original).area)
            if not snapped.is_empty
            else 0.0
        )

        rows.append(
            {
                "source_index": source_index,
                "load": float(record["load"]),
                "original_area": original_area,
                "snapped_area": snapped_area,
                "lost_area": lost_area,
                "gained_area": gained_area,
                "lost_fraction": (
                    lost_area / original_area if original_area > 0 else 0.0
                ),
                "gained_fraction": (
                    gained_area / original_area if original_area > 0 else 0.0
                ),
            }
        )

    return rows

