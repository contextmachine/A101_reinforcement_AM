from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import signal
import subprocess
import time
import traceback
from multiprocessing import get_context
from typing import Any, Mapping, Sequence

import numpy as np

try:  # package import
    from .select_min_density_rectangles_recipes import (
        _normalize_holds_axis,
        _normalize_mosaic,
        _rectangle_area_with_hold,
        _coerce_prepared_rectangle_problem,
        extend_prepared_original_indices,
        materialize_prepared_original_indices,
        prepare_rectangle_problem,
        save_prepared_rectangle_problem,
        load_prepared_rectangle_problem,
        verify_prepared_original_indices,
        select_min_density_rectangles,
    )
except ImportError:  # direct module import
    from select_min_density_rectangles_recipes import (
        _normalize_holds_axis,
        _normalize_mosaic,
        _rectangle_area_with_hold,
        _coerce_prepared_rectangle_problem,
        extend_prepared_original_indices,
        materialize_prepared_original_indices,
        prepare_rectangle_problem,
        save_prepared_rectangle_problem,
        load_prepared_rectangle_problem,
        verify_prepared_original_indices,
        select_min_density_rectangles,
    )

DATA_SCHEMA_VERSION = 3


def _normalized_recipes_json(recipes: Mapping[int, Sequence[int]] | None) -> str:
    normalized = {
        str(int(key)): sorted(int(value) for value in values)
        for key, values in (recipes or {}).items()
    }
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _update_hash_array(digest: Any, value: Any, dtype: str) -> None:
    array = np.ascontiguousarray(np.asarray(value, dtype=np.dtype(dtype)))
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.tobytes(order="C"))


def _problem_id(
    value_matrix: Sequence[Sequence[int]],
    xs: Sequence[float],
    ys: Sequence[float],
    rectangles: Sequence[Sequence[int]],
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
    holds: Mapping[int, float] | None,
    axis: str | None,
    cover_zero_cells: bool,
) -> str:
    digest = hashlib.sha256()
    if recipes:
        # Recipe variables are integer multiplicities in this model. Previous
        # binary-recipe incumbents must not be mixed into this solution pool.
        digest.update(b"rectangle-cover:3.3.0-recipe-multiplicity-v1")
    elif holds:
        # Preserve legacy-task cache compatibility when recipes are disabled.
        digest.update(b"rectangle-cover:3.1.0-holds-axis:recipes-holds-v1")
    else:
        digest.update(
            b"rectangle-cover:3.0.3-recipes-sparse-safe:"
            b"recipes-requirement-only-v2"
        )
    _update_hash_array(digest, value_matrix, "<i8")
    _update_hash_array(digest, xs, "<f8")
    _update_hash_array(digest, ys, "<f8")
    rectangle_array = np.asarray(rectangles, dtype=np.dtype("<i8"))
    if rectangle_array.size == 0:
        rectangle_array = rectangle_array.reshape(0, 5)
    else:
        rectangle_array = rectangle_array.reshape(-1, 5)
    _update_hash_array(digest, rectangle_array, "<i8")
    digest.update(
        json.dumps(
            [[int(key), float(value)] for key, value in sorted(densities.items())],
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(_normalized_recipes_json(recipes).encode("utf-8"))
    if holds:
        digest.update(
            json.dumps(
                [[int(key), float(value)] for key, value in sorted(holds.items())],
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update((axis or "").encode("ascii"))
    digest.update(b"1" if cover_zero_cells else b"0")
    return digest.hexdigest()


def _compact_solution_entry(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        indices = sorted(map(int, raw["indices"]))
    except (KeyError, TypeError, ValueError):
        return None
    compact: dict[str, Any] = {"indices": indices}
    try:
        cost = float(raw.get("cost", raw.get("total_cost")))
    except (TypeError, ValueError):
        cost = float("nan")
    if np.isfinite(cost):
        compact["cost"] = cost
    if raw.get("v") == 1 and bool(raw.get("optimal", raw.get("is_optimal", False))):
        compact.update(optimal=True, v=1)
    return compact


def _solution_candidates(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [entry for entry in raw if isinstance(entry, Mapping)]
    return []


def _solution_rank(entry: Mapping[str, Any]) -> tuple[int, float]:
    return (
        0 if bool(entry.get("optimal", entry.get("is_optimal", False))) else 1,
        float(entry.get("cost", entry.get("total_cost", float("inf")))),
    )


def _solution_entry(data: Mapping[str, Any], N: int) -> Mapping[str, Any] | None:
    candidates = [
        compact
        for entry in _solution_candidates(
            data.get("solutions", {}).get(str(int(N)))
        )
        if (compact := _compact_solution_entry(entry)) is not None
    ]
    return min(candidates, key=_solution_rank) if candidates else None


def _normalize_data(data: Mapping[str, Any] | None, problem_id: str) -> dict[str, Any]:
    """Migrate to a compact cache without deep-copying discarded history."""

    source = dict(data or {})
    old_problem_id = source.get("problem_id")
    if old_problem_id is not None and old_problem_id != problem_id:
        raise ValueError(
            "data относится к другой задаче: problem_id не совпадает с текущими входами"
        )

    # Hot path for data produced by this version: copy only the two mutable
    # top-level containers. Individual entries and their index lists are
    # immutable from the cache's point of view and need no repeated copying.
    if int(source.get("schema_version", -1)) == DATA_SCHEMA_VERSION:
        raw_solutions = source.get("solutions", {})
        raw_infeasible = source.get("infeasible", [])
        if isinstance(raw_solutions, Mapping) and isinstance(
            raw_infeasible, (list, tuple, set)
        ):
            solutions = dict(raw_solutions)
            infeasible = sorted(
                {
                    int(value)
                    for value in raw_infeasible
                    if not isinstance(value, bool)
                    and str(value).lstrip("-").isdigit()
                }
            )
            for n in infeasible:
                solutions.pop(str(n), None)
            return {
                "schema_version": DATA_SCHEMA_VERSION,
                "problem_id": problem_id,
                "solutions": solutions,
                "infeasible": infeasible,
            }

    solutions: dict[str, dict[str, Any]] = {}
    for raw_n, raw_entries in dict(source.get("solutions", {})).items():
        try:
            key = str(int(raw_n))
        except (TypeError, ValueError):
            continue
        candidates = [
            compact
            for entry in _solution_candidates(raw_entries)
            if (compact := _compact_solution_entry(entry)) is not None
        ]
        if candidates:
            solutions[key] = min(candidates, key=_solution_rank)

    # Solver/model validation changed in schema v3. Keep old incumbents as
    # feasible starts, but do not trust old optimal/infeasible certificates.
    infeasible: set[int] = set()

    return {
        "schema_version": DATA_SCHEMA_VERSION,
        "problem_id": problem_id,
        "solutions": solutions,
        "infeasible": sorted(infeasible),
    }


def _effective_density(
    class_id: int,
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
) -> float:
    # Recipe classes are requirement-only and cannot be selected as rectangles.
    if class_id in (recipes or {}):
        return float("inf")
    if class_id == 0 and class_id not in densities:
        return 0.0
    if class_id not in densities:
        raise ValueError(f"Для класса прямоугольника w={class_id} нет densities")
    return float(densities[class_id])


def _rectangle_metadata(
    xs: Sequence[float],
    ys: Sequence[float],
    rectangles: Sequence[Sequence[int]],
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
    holds: Mapping[int, float],
    axis: str | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rects = np.asarray(rectangles, dtype=np.int64)
    if rects.size == 0:
        rects = rects.reshape(0, 5)
    else:
        rects = rects.reshape(-1, 5)

    x_edges = np.r_[0.0, np.cumsum(np.asarray(xs, dtype=float))]
    y_edges = np.r_[0.0, np.cumsum(np.asarray(ys, dtype=float))]
    if len(rects):
        metrics = [
            _rectangle_area_with_hold(
                x_edges,
                y_edges,
                int(xmin),
                int(ymin),
                int(xmax),
                int(ymax),
                int(level),
                holds,
                axis,
            )
            for xmin, ymin, xmax, ymax, level in rects
        ]
        base_areas = np.asarray([item[0] for item in metrics], dtype=float)
        areas = np.asarray([item[1] for item in metrics], dtype=float)
        effective_densities = np.fromiter(
            (
                _effective_density(int(level), densities, recipes)
                for level in rects[:, 4]
            ),
            dtype=float,
            count=len(rects),
        )
        costs = areas * effective_densities
    else:
        base_areas = np.empty(0, dtype=float)
        areas = np.empty(0, dtype=float)
        effective_densities = np.empty(0, dtype=float)
        costs = np.empty(0, dtype=float)
    return rects, base_areas, areas, effective_densities, costs


def _class_layers(
    class_id: int,
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
) -> tuple[int, ...]:
    raw_layers = (recipes or {}).get(class_id)
    if raw_layers is not None:
        layers = tuple(sorted(int(value) for value in raw_layers))
        if not layers or any(
            layer != 0 and layer not in densities for layer in layers
        ):
            return ()
        return layers
    if class_id == 0:
        return (0,)
    if class_id in densities:
        return (class_id,)
    return ()


def _threshold_requirements(
    class_id: int,
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
) -> tuple[tuple[int, int], ...]:
    layers = _class_layers(class_id, densities, recipes)
    if not layers:
        return ()
    return tuple(
        (threshold, sum(layer >= threshold for layer in layers))
        for threshold in sorted(set(layers))
    )


def _candidate_multiplicity_limit(
    index: int,
    value_matrix: Sequence[Sequence[int]],
    rectangles: np.ndarray,
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
    cover_zero_cells: bool,
) -> int:
    """Maximum useful copies of one primitive rectangle in the recipe model."""

    if not recipes:
        return 1
    xmin, ymin, xmax, ymax, level_raw = map(int, rectangles[int(index)])
    level = int(level_raw)
    if level in recipes or (level != 0 and level not in densities):
        return 0

    requirements = np.asarray(value_matrix, dtype=np.int64)
    block = requirements[ymin : ymax + 1, xmin : xmax + 1]
    active = np.ones_like(block, dtype=bool) if cover_zero_cells else block > 0
    limit = 1  # every distinct candidate may still be used once as an exact-N filler
    for class_raw in np.unique(block[active]):
        for threshold, need in _threshold_requirements(
            int(class_raw), densities, recipes
        ):
            if level >= threshold:
                limit = max(limit, int(need))
    return limit


def _verify_original_indices(
    indices: Sequence[int],
    N: int,
    value_matrix: Sequence[Sequence[int]],
    rectangles: np.ndarray,
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
    cover_zero_cells: bool,
) -> bool:
    normalized = [int(index) for index in indices]
    if len(normalized) != N:
        return False
    if not recipes and len(set(normalized)) != N:
        return False
    if any(index < 0 or index >= len(rectangles) for index in normalized):
        return False

    if recipes:
        for index, count in Counter(normalized).items():
            if count > _candidate_multiplicity_limit(
                index,
                value_matrix,
                rectangles,
                densities,
                recipes,
                cover_zero_cells,
            ):
                return False

    requirements = np.asarray(value_matrix, dtype=np.int64)
    active = np.ones_like(requirements, dtype=bool) if cover_zero_cells else requirements > 0
    selected = rectangles[np.asarray(normalized, dtype=np.int64)]

    for y_raw, x_raw in np.argwhere(active):
        y, x = int(y_raw), int(x_raw)
        class_id = int(requirements[y, x])
        threshold_needs = _threshold_requirements(class_id, densities, recipes)
        if not threshold_needs:
            return False
        contains = (
            (selected[:, 0] <= x)
            & (selected[:, 2] >= x)
            & (selected[:, 1] <= y)
            & (selected[:, 3] >= y)
        )
        selected_here = selected[contains]
        for threshold, need in threshold_needs:
            supplied = 0
            for level_raw in selected_here[:, 4]:
                level = int(level_raw)
                if level in (recipes or {}) or (level != 0 and level not in densities):
                    return False
                supplied += int(level >= threshold)
            if supplied < need:
                return False
    return True



def _best_warm_start(
    data: Mapping[str, Any],
    N: int,
    value_matrix: Sequence[Sequence[int]],
    rectangles: np.ndarray,
    costs: np.ndarray,
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
    cover_zero_cells: bool,
    allow_cross_n: bool = False,
) -> tuple[list[int] | None, int | None, float | None]:
    """Use the same N by default; cross-N expansion is opt-in."""

    order = np.lexsort((np.arange(len(costs)), costs))
    selectable_order = [
        int(index_raw)
        for index_raw in order
        if np.isfinite(costs[int(index_raw)])
        and int(rectangles[int(index_raw), 4]) not in (recipes or {})
        and (
            int(rectangles[int(index_raw), 4]) == 0
            or int(rectangles[int(index_raw), 4]) in densities
        )
    ]
    source_ns = sorted(
        {
            int(raw_n)
            for raw_n in data.get("solutions", {})
            if str(raw_n).lstrip("-").isdigit()
            and (int(raw_n) == N or (allow_cross_n and int(raw_n) < N))
        },
        reverse=True,
    )

    for old_n in source_ns:
        entry = _solution_entry(data, old_n)
        if entry is None:
            continue
        raw_indices = entry.get("indices")
        try:
            indices = [int(index) for index in raw_indices]
        except (TypeError, ValueError):
            continue
        if not _verify_original_indices(
            indices,
            old_n,
            value_matrix,
            rectangles,
            densities,
            recipes,
            cover_zero_cells,
        ):
            continue

        if old_n < N:
            counts = Counter(indices)
            limit_cache: dict[int, int] = {}
            missing = N - len(indices)
            for index in selectable_order:
                if missing <= 0:
                    break
                limit = (
                    limit_cache.setdefault(
                        index,
                        _candidate_multiplicity_limit(
                            index,
                            value_matrix,
                            rectangles,
                            densities,
                            recipes,
                            cover_zero_cells,
                        ),
                    )
                    if recipes
                    else 1
                )
                capacity = limit - int(counts.get(index, 0))
                if capacity <= 0:
                    continue
                add = min(capacity, missing)
                indices.extend([index] * add)
                counts[index] += add
                missing -= add
            if missing:
                continue

        if not _verify_original_indices(
            indices,
            N,
            value_matrix,
            rectangles,
            densities,
            recipes,
            cover_zero_cells,
        ):
            continue
        total_cost = float(costs[np.asarray(indices, dtype=np.int64)].sum())
        return indices, old_n, total_cost

    return None, None, None

def _materialize_cached_result(
    entry: Mapping[str, Any],
    N: int,
    rectangles: np.ndarray,
    base_areas: np.ndarray,
    areas: np.ndarray,
    effective_densities: np.ndarray,
    costs: np.ndarray,
    problem_id: str,
    holds: Mapping[int, float],
    axis: str | None,
) -> dict[str, Any]:
    indices = sorted(int(index) for index in entry["indices"])
    details = []
    for index in indices:
        raw = tuple(map(int, rectangles[index]))
        details.append(
            {
                "index": index,
                "rectangle": raw,
                "w": raw[4],
                "density": float(effective_densities[index]),
                "base_area": float(base_areas[index]),
                "area": float(areas[index]),
                "hold": float(holds.get(int(raw[4]), 0.0)),
                "hold_axis": axis,
                "cost": float(costs[index]),
            }
        )
    total_cost = float(costs[np.asarray(indices, dtype=np.int64)].sum())
    counts = Counter(indices)
    multiplicities = [
        {
            "index": index,
            "rectangle": tuple(map(int, rectangles[index])),
            "count": int(count),
        }
        for index, count in sorted(counts.items())
    ]
    return {
        "rectangles": [tuple(map(int, rectangles[index])) for index in indices],
        "indices": indices,
        "details": details,
        "multiplicities": multiplicities,
        "total_cost": total_cost,
        "total_weighted_area": total_cost,
        "total_area": float(areas[np.asarray(indices, dtype=np.int64)].sum()),
        "total_base_area": float(base_areas[np.asarray(indices, dtype=np.int64)].sum()),
        "n_rectangles": len(indices),
        "n_requested": N,
        "status": "Optimal",
        "is_feasible": True,
        "is_optimal": True,
        "solver_proved_optimal": True,
        "infeasibility_proved": False,
        "reason": None,
        "stats": {
            "cache_hit": True,
            "problem_id": problem_id,
            "warm_start_from_N": N,
            "holds_used": bool(holds),
            "hold_axis": axis,
            "distinct_rectangles_selected": len(counts),
            "repeated_rectangles": len(indices) - len(counts),
        },
        "component_statuses": [],
    }




def _materialize_cached_infeasible(
    entry: Any,
    N: int,
    problem_id: str,
) -> dict[str, Any]:
    reason = entry.get("reason") if isinstance(entry, Mapping) else None
    return {
        "rectangles": None,
        "indices": None,
        "details": None,
        "multiplicities": None,
        "total_cost": None,
        "total_weighted_area": None,
        "total_area": None,
        "total_base_area": None,
        "n_rectangles": None,
        "n_requested": N,
        "status": "Infeasible",
        "is_feasible": False,
        "is_optimal": False,
        "solver_proved_optimal": False,
        "infeasibility_proved": True,
        "reason": reason or "Недопустимость ранее доказана",
        "stats": {
            "cache_hit": True,
            "problem_id": problem_id,
        },
        "component_statuses": [],
    }


def _cached_optimal_entry(
    data: Mapping[str, Any],
    N: int,
    value_matrix: Sequence[Sequence[int]],
    rectangles: np.ndarray,
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
    cover_zero_cells: bool,
) -> Mapping[str, Any] | None:
    entry = _solution_entry(data, N)
    if entry is None or not bool(entry.get("optimal")):
        return None
    if _verify_original_indices(
        entry["indices"],
        N,
        value_matrix,
        rectangles,
        densities,
        recipes,
        cover_zero_cells,
    ):
        return entry
    return None


def _register_result(
    data: dict[str, Any],
    result: Mapping[str, Any],
    N: int,
    pool_size: int = 1,
) -> None:
    """Keep one compact incumbent per N; ``pool_size`` is API compatibility."""

    key = str(int(N))
    if result.get("is_feasible") and result.get("indices") is not None:
        entry: dict[str, Any] = {
            "indices": sorted(int(index) for index in result["indices"]),
            "cost": float(result["total_cost"]),
        }
        if bool(result.get("is_optimal")):
            entry.update(optimal=True, v=1)

        previous = _solution_entry(data, N)
        replace = previous is None
        if previous is not None:
            previous_optimal = bool(previous.get("optimal"))
            current_optimal = bool(entry.get("optimal"))
            replace = (
                (current_optimal and not previous_optimal)
                or (
                    current_optimal == previous_optimal
                    and float(entry["cost"])
                    < float(previous.get("cost", float("inf")))
                )
            )
        if replace:
            data["solutions"][key] = entry
        data["infeasible"] = [
            value for value in data.get("infeasible", []) if int(value) != int(N)
        ]

    if result.get("infeasibility_proved"):
        data["infeasible"] = sorted(
            set(map(int, data.get("infeasible", []))) | {int(N)}
        )
        data["solutions"].pop(key, None)


def _append_run(
    data: dict[str, Any], run: Mapping[str, Any], max_runs: int
) -> None:
    # Schema v3 intentionally stores no run history. Kept as a no-op so old
    # callers and the worker control flow remain source-compatible.
    return None


def _worker(conn, call: dict[str, Any]) -> None:
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass
    try:
        # Always capture a feasible incumbent. The caller can still require
        # `result["is_optimal"]` before accepting it as final.
        call = dict(call)
        call["require_optimal"] = False
        result = select_min_density_rectangles(**call)
        conn.send(("ok", result))
    except BaseException:
        try:
            conn.send(("error", traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        conn.close()


def _kill_process_tree(process) -> None:
    if process.pid is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            import psutil

            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            parent.kill()
        except Exception:
            pass
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def _run_worker(call: dict[str, Any], timeout: float) -> tuple[str, Any, float]:
    ctx = get_context("spawn")
    parent, child = ctx.Pipe(False)
    process = ctx.Process(target=_worker, args=(child, call))
    started = time.monotonic()
    process.start()
    child.close()

    try:
        while True:
            elapsed = time.monotonic() - started
            remaining = timeout - elapsed
            if remaining <= 0:
                if parent.poll(0):
                    try:
                        status, payload = parent.recv()
                        process.join(timeout=2)
                        return status, payload, time.monotonic() - started
                    except EOFError:
                        pass
                _kill_process_tree(process)
                return "timeout", None, time.monotonic() - started

            if parent.poll(min(0.05, remaining)):
                try:
                    status, payload = parent.recv()
                except EOFError:
                    status, payload = "error", f"worker exited with code {process.exitcode}"
                process.join(timeout=2)
                if process.is_alive():
                    _kill_process_tree(process)
                return status, payload, time.monotonic() - started

            if not process.is_alive():
                if parent.poll(0):
                    try:
                        status, payload = parent.recv()
                    except EOFError:
                        status, payload = "error", f"worker exited with code {process.exitcode}"
                else:
                    status, payload = "error", f"worker exited with code {process.exitcode}"
                process.join(timeout=1)
                return status, payload, time.monotonic() - started
    finally:
        parent.close()


def _default_solver_time_limit(timeout: float) -> float:
    timeout = float(timeout)
    return max(0.0001, timeout - min(5.0, max(0.001, timeout * 0.05)))


def _timeout_result(
    *,
    N: int,
    problem_id: str,
    elapsed: float,
    solver_time_limit: float,
    warm_from_N: int | None,
    require_optimal: bool,
    prepared_stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "rectangles": None,
        "indices": None,
        "details": None,
        "multiplicities": None,
        "total_cost": None,
        "total_weighted_area": None,
        "total_area": None,
        "total_base_area": None,
        "n_rectangles": None,
        "n_requested": int(N),
        "status": "HardTimeout",
        "is_feasible": False,
        "is_optimal": False,
        "solver_proved_optimal": False,
        "infeasibility_proved": False,
        "termination_reason": "hard_timeout",
        "time_limit_reached": True,
        "has_incumbent": False,
        "incumbent_source": None,
        "solver_incumbent_recovered": False,
        "incumbent_is_solver_best": False,
        "meets_requested_optimality": False,
        "reason": (
            "Worker превысил жёсткий timeout; допустимый incumbent для возврата "
            "не был доступен до запуска solver"
        ),
        "stats": {
            **dict(prepared_stats or {}),
            "problem_id": problem_id,
            "hard_timeout": float(elapsed),
            "solver_time_limit": float(solver_time_limit),
            "warm_start_from_N": warm_from_N,
            "termination_reason": "hard_timeout",
            "time_limit_reached": True,
            "require_optimal_requested": bool(require_optimal),
            "meets_requested_optimality": False,
        },
        "component_statuses": [],
    }


def _mark_hard_timeout_fallback(
    result: dict[str, Any],
    *,
    elapsed: float,
    solver_time_limit: float,
    warm_from_N: int | None,
    prepared_used: bool,
    require_optimal: bool,
) -> dict[str, Any]:
    result.update(
        {
            "status": "Feasible",
            "is_feasible": True,
            "is_optimal": False,
            "solver_proved_optimal": False,
            "infeasibility_proved": False,
            "termination_reason": "hard_timeout",
            "time_limit_reached": True,
            "has_incumbent": True,
            "incumbent_source": "warm_start_fallback",
            # A killed command-line CBC cannot expose its latest private
            # incumbent through PuLP.  This is the verified pre-solve start.
            "solver_incumbent_recovered": False,
            "incumbent_is_solver_best": False,
            "meets_requested_optimality": False,
            "reason": (
                "Worker был принудительно остановлен; возвращён проверенный "
                "warm-start incumbent, а не скрытое состояние убитого CBC"
            ),
        }
    )
    stats = result.setdefault("stats", {})
    stats.update(
        {
            "cache_hit": False,
            "prepared_used": bool(prepared_used),
            "hard_timeout": float(elapsed),
            "solver_time_limit": float(solver_time_limit),
            "warm_start_from_N": warm_from_N,
            "termination_reason": "hard_timeout",
            "time_limit_reached": True,
            "incumbent_source": "warm_start_fallback",
            "solver_incumbent_recovered": False,
            "incumbent_is_solver_best": False,
            "require_optimal_requested": bool(require_optimal),
            "meets_requested_optimality": False,
        }
    )
    return result


def _ensure_job_result_metadata(
    result: dict[str, Any], *, incumbent_source: str | None
) -> dict[str, Any]:
    feasible = bool(result.get("is_feasible"))
    optimal = bool(result.get("is_optimal"))
    infeasible = bool(result.get("infeasibility_proved"))
    result.setdefault(
        "termination_reason",
        "optimal" if optimal else "infeasible" if infeasible else "solver_stopped" if feasible else "no_solution",
    )
    result.setdefault("time_limit_reached", False)
    result.setdefault("has_incumbent", feasible)
    result.setdefault("incumbent_source", incumbent_source if feasible else None)
    result.setdefault(
        "solver_incumbent_recovered",
        bool(feasible and incumbent_source == "solver"),
    )
    result.setdefault(
        "incumbent_is_solver_best",
        bool(feasible and incumbent_source == "solver"),
    )
    stats = result.setdefault("stats", {})
    stats.setdefault("termination_reason", result["termination_reason"])
    stats.setdefault("time_limit_reached", bool(result["time_limit_reached"]))
    stats.setdefault("incumbent_source", result.get("incumbent_source"))
    return result



def _prefer_better_known_incumbent(
    solver_result: dict[str, Any], known_result: dict[str, Any]
) -> dict[str, Any]:
    """Never accept an 'optimal' result above a verified feasible incumbent."""
    if solver_result.get("infeasibility_proved"):
        if known_result.get("is_feasible"):
            raise RuntimeError("Solver says infeasible, but a verified incumbent exists")
        return solver_result

    solver_cost, known_cost = solver_result.get("total_cost"), known_result.get("total_cost")
    solver_feasible = bool(solver_result.get("is_feasible"))
    better = not solver_feasible
    if solver_feasible and solver_cost is not None and known_cost is not None:
        tol = 1e-9 * max(1.0, abs(float(solver_cost)), abs(float(known_cost)))
        better = float(known_cost) < float(solver_cost) - tol
    if not better:
        return solver_result

    claimed_optimal = bool(solver_result.get("is_optimal"))
    solver_stats = dict(solver_result.get("stats", {}))
    known_result.update({
        "status": "Feasible",
        "is_feasible": True,
        "is_optimal": False,
        "solver_proved_optimal": False,
        "infeasibility_proved": False,
        "termination_reason": "optimality_conflict" if claimed_optimal else solver_result.get("termination_reason", "solver_stopped"),
        "time_limit_reached": bool(solver_result.get("time_limit_reached", False)),
        "has_incumbent": True,
        "incumbent_source": "known_pool",
        "solver_incumbent_recovered": solver_feasible,
        "incumbent_is_solver_best": False,
        "reason": "Solver claimed optimality above a cheaper verified incumbent" if claimed_optimal else "Returned a cheaper verified incumbent",
    })
    stats = known_result.setdefault("stats", {})
    stats.update(solver_stats)
    stats.update({
        "solver_returned_cost": None if solver_cost is None else float(solver_cost),
        "returned_known_incumbent_cost": None if known_cost is None else float(known_cost),
        "returned_better_known_incumbent": True,
        "solver_optimality_conflict": claimed_optimal,
        "incumbent_source": "known_pool",
    })
    return known_result

def _solve_rectangle_job_raw(
    value_matrix: Sequence[Sequence[int]],
    xs: Sequence[float],
    ys: Sequence[float],
    rectangles: Sequence[Sequence[int]],
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]] | None,
    data: Mapping[str, Any] | None,
    N: int,
    *,
    holds: Mapping[int, float] | None = None,
    axis: str | int | None = None,
    mosaic: Sequence[Sequence[int]] | None = None,
    timeout: float = 100.0,
    solver_time_limit: float | None = None,
    threads: int = 1,
    solver_msg: bool = False,
    require_optimal: bool = True,
    cover_zero_cells: bool = False,
    decompose: bool = True,
    greedy_warm_start_limit: int = 2_000,
    backend: str = "pulp",
    pulp_solver: str = "cbc",
    highs_options: Mapping[str, Any] | None = None,
    use_warm_start: bool = True,
    cross_n_warm_start: bool = False,
    return_best_on_timeout: bool = True,
    pool_size: int = 4,
    max_runs: int = 200,
    raise_worker_errors: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    Solve exact `N` in an isolated process and return `(result, updated_data)`.

    The worker always returns a feasible incumbent when the backend has one;
    inspect `result["is_optimal"]` when `require_optimal=True`. `data` contains
    only serializable solution-pool metadata and can be stored in Redis. In the
    recipe branch `indices` may contain the same original index several times;
    cached solutions and warm starts preserve these multiplicities.
    ``mosaic`` is an optional integer matrix with the same shape as
    ``value_matrix``; it proposes cells whose duplicate cover rows may be
    collapsed. The core refines it at all remaining rectangle boundaries, so
    using it does not change feasibility or the optimum.
    """
    if isinstance(N, bool) or int(N) != N or int(N) < 0:
        raise ValueError("N должен быть неотрицательным целым")
    N = int(N)
    timeout = float(timeout)
    if not np.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout должен быть положительным")
    if isinstance(threads, bool) or int(threads) != threads or int(threads) <= 0:
        raise ValueError("threads должен быть положительным целым")
    threads = int(threads)
    if pool_size <= 0 or max_runs <= 0:
        raise ValueError("pool_size и max_runs должны быть положительными")

    rectangle_list = [tuple(int(value) for value in rectangle) for rectangle in rectangles]
    density_map = dict(densities)
    density_map.setdefault(0, 0.0)
    recipes = dict(recipes or {})
    value_shape = np.asarray(value_matrix).shape
    if len(value_shape) != 2:
        raise ValueError("value_matrix должна быть двумерной")
    normalized_mosaic = _normalize_mosaic(
        mosaic, (int(value_shape[0]), int(value_shape[1]))
    )
    normalized_holds, hold_axis = _normalize_holds_axis(density_map, holds, axis)
    problem_id = _problem_id(
        value_matrix,
        xs,
        ys,
        rectangle_list,
        density_map,
        recipes,
        normalized_holds,
        hold_axis,
        cover_zero_cells,
    )
    data_normalize_started = time.monotonic()
    updated_data = _normalize_data(data, problem_id)
    data_normalize_seconds = time.monotonic() - data_normalize_started
    rects, base_areas, areas, effective_densities, costs = _rectangle_metadata(
        xs,
        ys,
        rectangle_list,
        density_map,
        recipes,
        normalized_holds,
        hold_axis,
    )

    cached_infeasible = N in set(map(int, updated_data.get("infeasible", [])))
    if cached_infeasible:
        result = _materialize_cached_infeasible({}, N, problem_id)
        result = _ensure_job_result_metadata(result, incumbent_source=None)
        result["stats"].update(
            {
                "mosaic_used": normalized_mosaic is not None,
                "mosaic_not_evaluated_due_to_cache": normalized_mosaic is not None,
                "data_normalize_seconds": float(data_normalize_seconds),
            }
        )
        _append_run(
            updated_data,
            {
                "N": N,
                "status": "CachedInfeasible",
                "elapsed": 0.0,
                "timestamp": time.time(),
            },
            max_runs,
        )
        return result, updated_data

    cached = _cached_optimal_entry(
        updated_data,
        N,
        value_matrix,
        rects,
        density_map,
        recipes,
        cover_zero_cells,
    )
    if cached is not None:
        result = _materialize_cached_result(
            cached,
            N,
            rects,
            base_areas,
            areas,
            effective_densities,
            costs,
            problem_id,
            normalized_holds,
            hold_axis,
        )
        result = _ensure_job_result_metadata(result, incumbent_source="cache")
        result["stats"].update(
            {
                "mosaic_used": normalized_mosaic is not None,
                "mosaic_not_evaluated_due_to_cache": normalized_mosaic is not None,
                "data_normalize_seconds": float(data_normalize_seconds),
            }
        )
        _append_run(
            updated_data,
            {
                "N": N,
                "status": "CachedOptimal",
                "elapsed": 0.0,
                "timestamp": time.time(),
            },
            max_runs,
        )
        return result, updated_data

    warm_lookup_started = time.monotonic()
    if use_warm_start:
        warm_indices, warm_from_N, warm_cost = _best_warm_start(
            updated_data,
            N,
            value_matrix,
            rects,
            costs,
            density_map,
            recipes,
            cover_zero_cells,
            allow_cross_n=bool(cross_n_warm_start),
        )
    else:
        warm_indices = warm_from_N = warm_cost = None
    warm_lookup_seconds = time.monotonic() - warm_lookup_started

    if solver_time_limit is None:
        solver_time_limit = _default_solver_time_limit(timeout)
    else:
        solver_time_limit = float(solver_time_limit)
        if not np.isfinite(solver_time_limit) or solver_time_limit <= 0:
            raise ValueError("solver_time_limit должен быть положительным или None")
        solver_time_limit = min(
            solver_time_limit, _default_solver_time_limit(timeout)
        )

    call: dict[str, Any] = {
        "value_matrix": value_matrix,
        "xs": xs,
        "ys": ys,
        "rectangles": rectangle_list,
        "densities": density_map,
        "recipes": recipes,
        "holds": normalized_holds,
        "axis": hold_axis,
        "mosaic": normalized_mosaic,
        "n": N,
        "initial_indices": warm_indices,
        "cover_zero_cells": cover_zero_cells,
        "solver_msg": solver_msg,
        "time_limit": solver_time_limit,
        "threads": threads,
        "decompose": decompose,
        "greedy_warm_start_limit": greedy_warm_start_limit,
        "backend": backend,
        "pulp_solver": pulp_solver,
        "highs_options": dict(highs_options or {}),
    }

    status, payload, elapsed = _run_worker(call, timeout)
    if status == "timeout":
        if not return_best_on_timeout:
            return None, updated_data
        if warm_indices is not None:
            result = _materialize_cached_result(
                {"indices": warm_indices, "cost": warm_cost},
                N,
                rects,
                base_areas,
                areas,
                effective_densities,
                costs,
                problem_id,
                normalized_holds,
                hold_axis,
            )
            result = _mark_hard_timeout_fallback(
                result,
                elapsed=elapsed,
                solver_time_limit=solver_time_limit,
                warm_from_N=warm_from_N,
                prepared_used=False,
                require_optimal=require_optimal,
            )
            _register_result(updated_data, result, N, int(pool_size))
            return result, updated_data
        return (
            _timeout_result(
                N=N,
                problem_id=problem_id,
                elapsed=elapsed,
                solver_time_limit=solver_time_limit,
                warm_from_N=warm_from_N,
                require_optimal=require_optimal,
            ),
            updated_data,
        )

    if status == "error":
        _append_run(
            updated_data,
            {
                "N": N,
                "status": "WorkerError",
                "elapsed": elapsed,
                "warm_start_from_N": warm_from_N,
                "error": str(payload),
                "timestamp": time.time(),
            },
            max_runs,
        )
        if raise_worker_errors:
            raise RuntimeError(f"Ошибка worker при N={N}:\n{payload}")
        return None, updated_data

    result = _ensure_job_result_metadata(dict(payload), incumbent_source="solver")
    stats = result.setdefault("stats", {})
    stats.update(
        {
            "problem_id": problem_id,
            "cache_hit": False,
            "warm_start_from_N": warm_from_N,
            "warm_start_cost": warm_cost,
            "warm_start_lookup_seconds": float(warm_lookup_seconds),
            "cross_n_warm_start": bool(cross_n_warm_start),
            "warm_start_enabled": bool(use_warm_start),
            "data_solution_count": len(updated_data.get("solutions", {})),
            "data_normalize_seconds": float(data_normalize_seconds),
            "hard_timeout": timeout,
            "solver_time_limit": solver_time_limit,
            "worker_elapsed_seconds": float(elapsed),
            "require_optimal_requested": bool(require_optimal),
            "holds_used": bool(normalized_holds),
            "hold_axis": hold_axis,
            "mosaic_used": normalized_mosaic is not None,
            "meets_requested_optimality": bool(
                not require_optimal
                or result.get("is_optimal")
                or result.get("infeasibility_proved")
            ),
        }
    )
    if warm_indices is not None and warm_cost is not None:
        known = _materialize_cached_result(
            {"indices": warm_indices, "cost": warm_cost},
            N,
            rects,
            base_areas,
            areas,
            effective_densities,
            costs,
            problem_id,
            normalized_holds,
            hold_axis,
        )
        result = _prefer_better_known_incumbent(result, known)
    _register_result(updated_data, result, N, int(pool_size))
    _append_run(
        updated_data,
        {
            "N": N,
            "status": str(result.get("status", "Unknown")),
            "elapsed": elapsed,
            "is_feasible": bool(result.get("is_feasible")),
            "is_optimal": bool(result.get("is_optimal")),
            "infeasibility_proved": bool(result.get("infeasibility_proved")),
            "warm_start_from_N": warm_from_N,
            "timestamp": time.time(),
        },
        max_runs,
    )
    return result, updated_data




def _cached_optimal_entry_prepared(
    data: Mapping[str, Any],
    N: int,
    prepared: Mapping[str, Any],
) -> dict[str, Any] | None:
    entry = _solution_entry(data, N)
    if entry is None or not bool(entry.get("optimal")):
        return None
    indices = entry.get("indices")
    if indices is not None and verify_prepared_original_indices(prepared, indices, N):
        return dict(entry)
    return None


def _best_warm_start_prepared(
    data: Mapping[str, Any],
    N: int,
    prepared: Mapping[str, Any],
    allow_cross_n: bool = False,
) -> tuple[list[int] | None, int | None, float | None]:
    """Use the same N by default; cross-N expansion is opt-in."""

    source_ns = sorted(
        {
            int(raw_n)
            for raw_n in data.get("solutions", {})
            if str(raw_n).lstrip("-").isdigit()
            and (int(raw_n) == N or (allow_cross_n and int(raw_n) < N))
        },
        reverse=True,
    )
    for source_n in source_ns:
        entry = _solution_entry(data, source_n)
        if entry is None:
            continue
        indices = entry.get("indices")
        if indices is None:
            continue
        extended = extend_prepared_original_indices(prepared, indices, N)
        if extended is None:
            continue
        target_indices, target_cost = extended
        return target_indices, source_n, float(target_cost)
    return None, None, None

def _solve_rectangle_job_prepared(
    prepared_source,
    data: Mapping[str, Any] | None,
    N: int,
    *,
    timeout: float,
    solver_time_limit: float | None,
    threads: int,
    solver_msg: bool,
    require_optimal: bool,
    greedy_warm_start_limit: int,
    backend: str,
    pulp_solver: str,
    highs_options: Mapping[str, Any] | None,
    use_warm_start: bool,
    cross_n_warm_start: bool,
    return_best_on_timeout: bool,
    pool_size: int,
    max_runs: int,
    raise_worker_errors: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if isinstance(N, bool) or int(N) != N or int(N) < 0:
        raise ValueError("N должен быть неотрицательным целым")
    N = int(N)
    timeout = float(timeout)
    if not np.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout должен быть положительным")
    if isinstance(threads, bool) or int(threads) != threads or int(threads) <= 0:
        raise ValueError("threads должен быть положительным целым")
    threads = int(threads)
    if pool_size <= 0 or max_runs <= 0:
        raise ValueError("pool_size и max_runs должны быть положительными")

    prepared = _coerce_prepared_rectangle_problem(prepared_source)
    max_n = prepared.get("max_n")
    if max_n is not None and N > int(max_n):
        raise ValueError(
            f"prepared рассчитан для max_n={max_n}, но запрошено N={N}"
        )
    problem_id = str(prepared["problem_id"])
    data_normalize_started = time.monotonic()
    updated_data = _normalize_data(data, problem_id)
    data_normalize_seconds = time.monotonic() - data_normalize_started

    cached_infeasible = N in set(map(int, updated_data.get("infeasible", [])))
    if cached_infeasible:
        result = _materialize_cached_infeasible({}, N, problem_id)
        result = _ensure_job_result_metadata(result, incumbent_source=None)
        result["stats"].update(
            {
                **dict(prepared.get("stats", {})),
                "prepared_used": True,
                "cache_hit": True,
                "data_normalize_seconds": float(data_normalize_seconds),
            }
        )
        _append_run(
            updated_data,
            {"N": N, "status": "CachedInfeasible", "elapsed": 0.0, "timestamp": time.time()},
            max_runs,
        )
        return result, updated_data

    cached = _cached_optimal_entry_prepared(updated_data, N, prepared)
    if cached is not None:
        result = materialize_prepared_original_indices(
            prepared,
            cached["indices"],
            n_requested=N,
            status="Optimal",
            is_optimal=True,
        )
        result = _ensure_job_result_metadata(result, incumbent_source="cache")
        result["stats"].update(
            {
                "problem_id": problem_id,
                "cache_hit": True,
                "prepared_used": True,
                "require_optimal_requested": bool(require_optimal),
                "meets_requested_optimality": True,
                "data_normalize_seconds": float(data_normalize_seconds),
            }
        )
        _append_run(
            updated_data,
            {"N": N, "status": "CachedOptimal", "elapsed": 0.0, "timestamp": time.time()},
            max_runs,
        )
        return result, updated_data

    warm_lookup_started = time.monotonic()
    if use_warm_start:
        warm_indices, warm_from_N, warm_cost = _best_warm_start_prepared(
            updated_data, N, prepared, allow_cross_n=bool(cross_n_warm_start)
        )
    else:
        warm_indices = warm_from_N = warm_cost = None
    warm_lookup_seconds = time.monotonic() - warm_lookup_started

    if solver_time_limit is None:
        solver_time_limit = _default_solver_time_limit(timeout)
    else:
        solver_time_limit = float(solver_time_limit)
        if not np.isfinite(solver_time_limit) or solver_time_limit <= 0:
            raise ValueError("solver_time_limit должен быть положительным или None")
        solver_time_limit = min(
            solver_time_limit, _default_solver_time_limit(timeout)
        )

    # Passing a path is preferable on Windows/cluster nodes: the spawn process
    # receives a tiny string and loads the already-prepared pickle locally.
    call = {
        "prepared": prepared_source,
        "n": N,
        "initial_indices": warm_indices,
        "solver_msg": solver_msg,
        "time_limit": solver_time_limit,
        "threads": threads,
        "greedy_warm_start_limit": greedy_warm_start_limit,
        "backend": backend,
        "pulp_solver": pulp_solver,
        "highs_options": dict(highs_options or {}),
    }
    status, payload, elapsed = _run_worker(call, timeout)
    if status == "timeout":
        if not return_best_on_timeout:
            return None, updated_data
        if warm_indices is not None:
            result = materialize_prepared_original_indices(
                prepared,
                warm_indices,
                n_requested=N,
                status="Feasible",
                is_optimal=False,
            )
            result = _mark_hard_timeout_fallback(
                result,
                elapsed=elapsed,
                solver_time_limit=solver_time_limit,
                warm_from_N=warm_from_N,
                prepared_used=True,
                require_optimal=require_optimal,
            )
            _register_result(updated_data, result, N, int(pool_size))
            return result, updated_data
        return (
            _timeout_result(
                N=N,
                problem_id=problem_id,
                elapsed=elapsed,
                solver_time_limit=solver_time_limit,
                warm_from_N=warm_from_N,
                require_optimal=require_optimal,
                prepared_stats={**dict(prepared.get("stats", {})), "prepared_used": True},
            ),
            updated_data,
        )
    if status == "error":
        _append_run(
            updated_data,
            {
                "N": N,
                "status": "WorkerError",
                "elapsed": elapsed,
                "warm_start_from_N": warm_from_N,
                "prepared_used": True,
                "error": str(payload),
                "timestamp": time.time(),
            },
            max_runs,
        )
        if raise_worker_errors:
            raise RuntimeError(f"Ошибка worker при N={N}:\n{payload}")
        return None, updated_data

    result = _ensure_job_result_metadata(dict(payload), incumbent_source="solver")
    stats = result.setdefault("stats", {})
    stats.update(
        {
            "problem_id": problem_id,
            "cache_hit": False,
            "prepared_used": True,
            "warm_start_from_N": warm_from_N,
            "warm_start_cost": warm_cost,
            "warm_start_lookup_seconds": float(warm_lookup_seconds),
            "cross_n_warm_start": bool(cross_n_warm_start),
            "warm_start_enabled": bool(use_warm_start),
            "data_solution_count": len(updated_data.get("solutions", {})),
            "data_normalize_seconds": float(data_normalize_seconds),
            "hard_timeout": timeout,
            "solver_time_limit": solver_time_limit,
            "worker_elapsed_seconds": float(elapsed),
            "require_optimal_requested": bool(require_optimal),
            "meets_requested_optimality": bool(
                not require_optimal
                or result.get("is_optimal")
                or result.get("infeasibility_proved")
            ),
        }
    )
    if warm_indices is not None and warm_cost is not None:
        known = materialize_prepared_original_indices(
            prepared,
            warm_indices,
            n_requested=N,
            status="Feasible",
            is_optimal=False,
        )
        result = _prefer_better_known_incumbent(result, known)
    _register_result(updated_data, result, N, int(pool_size))
    _append_run(
        updated_data,
        {
            "N": N,
            "status": str(result.get("status", "Unknown")),
            "elapsed": elapsed,
            "is_feasible": bool(result.get("is_feasible")),
            "is_optimal": bool(result.get("is_optimal")),
            "infeasibility_proved": bool(result.get("infeasibility_proved")),
            "warm_start_from_N": warm_from_N,
            "prepared_used": True,
            "timestamp": time.time(),
        },
        max_runs,
    )
    return result, updated_data


def solve_rectangle_job(
    value_matrix: Sequence[Sequence[int]] | None = None,
    xs: Sequence[float] | None = None,
    ys: Sequence[float] | None = None,
    rectangles: Sequence[Sequence[int]] | None = None,
    densities: Mapping[int, float] | None = None,
    recipes: Mapping[int, Sequence[int]] | None = None,
    data: Mapping[str, Any] | None = None,
    N: int | None = None,
    *,
    prepared=None,
    holds: Mapping[int, float] | None = None,
    axis: str | int | None = None,
    mosaic: Sequence[Sequence[int]] | None = None,
    timeout: float = 100.0,
    solver_time_limit: float | None = None,
    threads: int = 1,
    solver_msg: bool = False,
    require_optimal: bool = True,
    cover_zero_cells: bool = False,
    decompose: bool = True,
    greedy_warm_start_limit: int = 2_000,
    backend: str = "pulp",
    pulp_solver: str = "cbc",
    highs_options: Mapping[str, Any] | None = None,
    use_warm_start: bool = True,
    cross_n_warm_start: bool = False,
    return_best_on_timeout: bool = True,
    pool_size: int = 4,
    max_runs: int = 200,
    raise_worker_errors: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Run one exact N in a killable worker, optionally from a prepared model.

    New repeated-N usage::

        prepared = prepare_rectangle_problem(..., max_n=87)
        save_prepared_rectangle_problem(prepared, "problem.prepared.pkl")
        result, data = solve_rectangle_job(
            prepared="problem.prepared.pkl", data=data, N=30, timeout=100
        )

    A prepared path avoids re-pickling a large model into every Windows spawn
    worker and is also convenient on shared cluster storage. ``use_warm_start``
    disables only MIP-start lookup; exact optimal/infeasible cache hits remain.
    The CBC soft limit is kept below the hard worker timeout so CBC can write
    and PuLP can read its incumbent. With ``return_best_on_timeout=True`` a
    soft-limit incumbent is returned normally; after a hard kill the verified
    warm start is returned when available, otherwise a structured HardTimeout
    result is returned. ``pool_size`` and ``max_runs`` remain only for source
    compatibility; schema v2 stores no pools or run history.

    Use ``backend="pulp", pulp_solver="cbc"`` for CBC,
    ``backend="pulp", pulp_solver="highs"`` for synchronous PuLP/HiGHS, or
    ``backend="highs"`` for the direct highspy backend. Live improving
    solutions are exposed by :mod:`rectangle_solver_stream`.
    """
    if N is None:
        raise ValueError("N обязателен")
    if prepared is not None:
        return _solve_rectangle_job_prepared(
            prepared,
            data,
            int(N),
            timeout=timeout,
            solver_time_limit=solver_time_limit,
            threads=threads,
            solver_msg=solver_msg,
            require_optimal=require_optimal,
            greedy_warm_start_limit=greedy_warm_start_limit,
            backend=backend,
            pulp_solver=pulp_solver,
            highs_options=highs_options,
            use_warm_start=bool(use_warm_start),
            cross_n_warm_start=bool(cross_n_warm_start),
            return_best_on_timeout=bool(return_best_on_timeout),
            pool_size=pool_size,
            max_runs=max_runs,
            raise_worker_errors=raise_worker_errors,
        )

    if value_matrix is None or xs is None or ys is None or rectangles is None or densities is None:
        raise ValueError(
            "Без prepared необходимо передать value_matrix, xs, ys, rectangles и densities"
        )
    return _solve_rectangle_job_raw(
        value_matrix=value_matrix,
        xs=xs,
        ys=ys,
        rectangles=rectangles,
        densities=densities,
        recipes=recipes,
        data=data,
        N=int(N),
        holds=holds,
        axis=axis,
        mosaic=mosaic,
        timeout=timeout,
        solver_time_limit=solver_time_limit,
        threads=threads,
        solver_msg=solver_msg,
        require_optimal=require_optimal,
        cover_zero_cells=cover_zero_cells,
        decompose=decompose,
        greedy_warm_start_limit=greedy_warm_start_limit,
        backend=backend,
        pulp_solver=pulp_solver,
        highs_options=highs_options,
        use_warm_start=bool(use_warm_start),
        cross_n_warm_start=bool(cross_n_warm_start),
        return_best_on_timeout=bool(return_best_on_timeout),
        pool_size=pool_size,
        max_runs=max_runs,
        raise_worker_errors=raise_worker_errors,
    )


prepare_rectangle_job = prepare_rectangle_problem
select_min_density_rectangles_job = solve_rectangle_job
