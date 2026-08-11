from __future__ import annotations

__version__ = "0.3.0"

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


def quantize_rectilinear_loads(
    records: Iterable[Mapping[str, Any]],
    target_cells_x: int | None,
    target_cells_y: int | None,
    *,
    method: str = "auto",
    exact_coordinate_limit: int = 2000,
    exact_work_limit: int = 120_000_000,
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
):
    """
    Main entry point.

    ``method``:
      - "exact": exact DP for the surrogate objective;
      - "greedy": scalable adjacent merging;
      - "auto": exact only when the estimated work is acceptable.

    Passing ``None`` for either target selects the minimum feasible
    number of cells for that axis.

    Holes are protected by default. Use ``min_hole_area`` or
    ``min_hole_area_fraction`` to ignore insignificant holes, or set
    ``preserve_holes=False`` to restore the legacy behavior.
    """

    records = list(records)
    x_events, y_events = build_axis_events(
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

    minimum_cells_x = minimum_feasible_grid_lines(x_events) - 1
    minimum_cells_y = minimum_feasible_grid_lines(y_events) - 1
    if target_cells_x is None:
        target_cells_x = minimum_cells_x
    if target_cells_y is None:
        target_cells_y = minimum_cells_y

    def solve(events: AxisEvents, target_cells: int):
        selected_method = method
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

    (
        (x_lines, x_mapping, objective_x, x_cuts),
        method_x,
    ) = solve(x_events, target_cells_x)
    (
        (y_lines, y_mapping, objective_y, y_cuts),
        method_y,
    ) = solve(y_events, target_cells_y)

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

    return {
        "x_lines": x_lines,
        "y_lines": y_lines,
        "x_original": x_events.coords,
        "y_original": y_events.coords,
        "x_mapping": x_mapping,
        "y_mapping": y_mapping,
        "snapped": snapped_records,
        "objective_x": objective_x,
        "objective_y": objective_y,
        "method_x": method_x,
        "method_y": method_y,
        "x_cuts": x_cuts,
        "y_cuts": y_cuts,
        "minimum_cells_x": minimum_cells_x,
        "minimum_cells_y": minimum_cells_y,
        "protected_hole_spans_x": (
            0 if x_events.protected_spans is None else len(x_events.protected_spans)
        ),
        "protected_hole_spans_y": (
            0 if y_events.protected_spans is None else len(y_events.protected_spans)
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

