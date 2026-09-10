from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import os
import pickle
from time import monotonic
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

__version__ = "5.1.0-validated-compact"


class _DSU:
    """Минимальная структура disjoint-set для разбиения set-cover на компоненты."""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        a = self.find(a)
        b = self.find(b)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def _import_pulp():
    try:
        import pulp
    except ImportError as exc:  # pragma: no cover - зависит от окружения пользователя
        raise ImportError(
            "Для backend='pulp' установите PuLP и CBC, например: "
            "python -m pip install pulp"
        ) from exc
    return pulp


def _import_highspy():
    try:
        import highspy
    except ImportError as exc:  # pragma: no cover - depends on user environment
        raise ImportError(
            "Для backend='highs' или pulp_solver='highs' установите highspy: "
            "python -m pip install highspy"
        ) from exc
    return highspy


def _cbc_solver(
    *, msg: bool, time_limit: float | None, threads: int | None, warm_start: bool
):
    """Создаёт CBC с нулевым gap и без лимита, если time_limit=None."""

    pulp = _import_pulp()
    solver_class = getattr(pulp, "PULP_CBC_CMD", None)
    if solver_class is None:
        solver_class = pulp.COIN_CMD

    kwargs: dict[str, Any] = {
        "msg": msg,
        "timeLimit": time_limit,
        "gapRel": 0.0,
        "gapAbs": 0.0,
        "warmStart": warm_start,
        "keepFiles": False,
    }
    if threads is not None:
        kwargs["threads"] = threads
    return solver_class(**kwargs)


def _normalize_pulp_solver_name(value: str | None) -> str:
    name = str(value or "cbc").strip().lower().replace("-", "_")
    aliases = {
        "coin": "cbc",
        "coin_cmd": "cbc",
        "pulp_cbc_cmd": "cbc",
        "highs_cmd": "highs",
        "highspy": "highs",
    }
    name = aliases.get(name, name)
    if name not in {"cbc", "highs"}:
        raise ValueError("pulp_solver должен быть равен 'cbc' или 'highs'")
    return name


def _make_pulp_solver(
    *,
    name: str,
    msg: bool,
    time_limit: float | None,
    threads: int | None,
    warm_start: bool,
    highs_options: Mapping[str, Any] | None = None,
):
    name = _normalize_pulp_solver_name(name)
    if name == "cbc":
        return _cbc_solver(
            msg=msg,
            time_limit=time_limit,
            threads=threads,
            warm_start=warm_start,
        )

    pulp = _import_pulp()
    # Prefer the in-process highspy API. It exposes better status information
    # and avoids command-line solution files. Older PuLP versions may only
    # provide HiGHS_CMD, which remains a valid synchronous fallback.
    solver_class = getattr(pulp, "HiGHS", None)
    if solver_class is None:
        solver_class = getattr(pulp, "HiGHS_CMD", None)
    if solver_class is None:
        raise ImportError(
            "Текущая версия PuLP не содержит HiGHS/HiGHS_CMD. "
            "Обновите PuLP и установите highspy."
        )

    highs_options = dict(highs_options or {})
    if threads is not None and int(threads) > 1:
        highs_options.setdefault("parallel", "on")
    kwargs: dict[str, Any] = {
        "msg": msg,
        "timeLimit": time_limit,
        "gapRel": 0.0,
        "gapAbs": 0.0,
        "warmStart": warm_start,
    }
    if threads is not None:
        kwargs["threads"] = int(threads)
    if getattr(solver_class, "__name__", "") == "HiGHS_CMD":
        kwargs["keepFiles"] = False
        if highs_options:
            kwargs["options"] = [f"{key}={value}" for key, value in highs_options.items()]
    else:
        # PuLP's in-process HiGHS forwards extra named parameters directly to
        # highspy. Explicit exactness/time/thread settings above take priority.
        extra = dict(highs_options)
        extra.update(kwargs)
        kwargs = extra

    # PuLP's HiGHS signatures evolved. Remove only optional features rejected
    # by an older wrapper, preserving exact zero relative gap whenever possible.
    optional = ["warmStart", "gapAbs", "threads", "keepFiles"]
    while True:
        try:
            return solver_class(**kwargs)
        except TypeError as exc:
            text = str(exc)
            key = next((item for item in optional if item in kwargs and item in text), None)
            if key is None:
                raise
            kwargs.pop(key, None)



def _solver_termination_metadata(
    *,
    proved_optimal: bool,
    proved_infeasible: bool,
    feasible: bool,
    status: Any,
    solution_status: Any,
    time_limit: float | None,
    solver_seconds: float | None,
    explicit_time_limit_reached: bool | None = None,
) -> dict[str, Any]:
    """Classify a solver stop without claiming more than the backend proves."""

    text = f"{status} {solution_status}".lower()
    textual_time_limit = any(
        token in text
        for token in (
            "time limit",
            "timelimit",
            "maximum time",
            "stopped on time",
            "time_limit",
        )
    )
    near_limit = False
    if time_limit is not None and solver_seconds is not None:
        # Allow for coarse clocks and for a small amount of post-solve I/O.
        near_limit = float(solver_seconds) >= max(0.0, 0.90 * float(time_limit) - 0.05)

    if proved_optimal:
        reason = "optimal"
        limit_reached = False
    elif proved_infeasible:
        reason = "infeasible"
        limit_reached = False
    else:
        if explicit_time_limit_reached is None:
            limit_reached = bool(
                time_limit is not None
                and (textual_time_limit or near_limit or str(status).lower() == "stopped")
            )
        else:
            limit_reached = bool(explicit_time_limit_reached)
        if limit_reached:
            reason = "time_limit"
        elif feasible:
            reason = "solver_stopped"
        else:
            reason = "no_solution"

    return {
        "termination_reason": reason,
        "time_limit_reached": bool(limit_reached),
        "has_incumbent": bool(feasible),
    }


def _normalize_holds_axis(
    densities: Mapping[int, float],
    holds: Mapping[int, float] | None,
    axis: str | int | None,
) -> tuple[dict[int, float], str | None]:
    """Validate hold distances and return a canonical axis (``x`` or ``y``)."""

    if not holds:
        return {}, None

    normalized: dict[int, float] = {}
    for raw_key, raw_value in holds.items():
        if isinstance(raw_key, bool) or int(raw_key) != raw_key:
            raise ValueError("Ключи holds должны быть целыми")
        key = int(raw_key)
        value = float(raw_value)
        if key <= 0:
            raise ValueError("Ключи holds должны совпадать с densities без класса 0")
        if not np.isfinite(value) or value < 0:
            raise ValueError("Значения holds должны быть конечными и неотрицательными")
        normalized[key] = value

    density_keys: set[int] = set()
    for raw_key in densities:
        if isinstance(raw_key, bool) or int(raw_key) != raw_key:
            raise ValueError("Ключи densities должны быть целыми")
        key = int(raw_key)
        if key != 0:
            density_keys.add(key)

    if set(normalized) != density_keys:
        raise ValueError(
            "Ключи holds должны точно совпадать с ключами densities, кроме 0; "
            f"ожидались {sorted(density_keys)}, получены {sorted(normalized)}"
        )

    if isinstance(axis, bool):
        raise ValueError("axis должен быть 'x'/'y' либо 1/0")
    if isinstance(axis, str):
        canonical = axis.strip().lower()
        if canonical not in {"x", "y"}:
            raise ValueError("axis должен быть 'x' или 'y'")
    elif isinstance(axis, (int, np.integer)) and int(axis) in {0, 1}:
        canonical = "y" if int(axis) == 0 else "x"
    else:
        raise ValueError("axis обязателен при непустом holds и должен быть 'x'/'y' либо 1/0")

    return normalized, canonical


def _rectangle_area_with_hold(
    x_edges: np.ndarray,
    y_edges: np.ndarray,
    xmin: int,
    ymin: int,
    xmax: int,
    ymax: int,
    level: int,
    holds: Mapping[int, float],
    axis: str | None,
) -> tuple[float, float, float]:
    """Return ``(base_area, objective_area, hold)`` for one rectangle."""

    x0 = float(x_edges[xmin])
    x1 = float(x_edges[xmax + 1])
    y0 = float(y_edges[ymin])
    y1 = float(y_edges[ymax + 1])
    width = x1 - x0
    height = y1 - y0
    base_area = width * height
    hold = float(holds.get(level, 0.0))

    if axis == "x" and hold > 0:
        x0 = max(0.0, x0 - hold)
        x1 = min(float(x_edges[-1]), x1 + hold)
    elif axis == "y" and hold > 0:
        y0 = max(0.0, y0 - hold)
        y1 = min(float(y_edges[-1]), y1 + hold)

    return base_area, (x1 - x0) * (y1 - y0), hold


def _normalize_mosaic(
    mosaic: Sequence[Sequence[int]] | None,
    shape: tuple[int, int],
) -> np.ndarray | None:
    """Validate an optional integer mosaic and return an ``int64`` array."""

    if mosaic is None:
        return None
    raw = np.asarray(mosaic)
    if raw.shape != shape:
        raise ValueError(
            f"mosaic должна иметь форму {shape}, получена {raw.shape}"
        )
    try:
        numeric = np.asarray(raw, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("mosaic должна содержать целые идентификаторы") from exc
    if not np.all(np.isfinite(numeric)) or not np.all(numeric == np.floor(numeric)):
        raise ValueError("mosaic должна содержать конечные целые идентификаторы")
    return numeric.astype(np.int64, copy=False)


def _build_mosaic_units(
    *,
    active_mask: np.ndarray,
    mosaic: np.ndarray,
    profile_grid: np.ndarray,
    candidates: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Refine proposed mosaic cells into exact, rectangle-indistinguishable units.

    The supplied labels are never merged with each other. They are split at
    requirement-profile changes, disconnected pieces, and every boundary of
    every remaining selectable rectangle. Therefore replacing all constraints
    inside one returned unit by a single row does not change the feasible set.
    """

    ny, nx = active_mask.shape
    # Difference arrays mark every rectangle edge in O(number_of_rectangles)
    # instead of touching every grid segment along every perimeter.
    vertical_diff = np.zeros((ny + 1, max(0, nx - 1)), dtype=np.int32)
    horizontal_diff = np.zeros((max(0, ny - 1), nx + 1), dtype=np.int32)
    for candidate in candidates:
        xmin, ymin, xmax, ymax, _level = candidate["rect"]
        if xmin > 0:
            edge = xmin - 1
            vertical_diff[ymin, edge] += 1
            vertical_diff[ymax + 1, edge] -= 1
        if xmax < nx - 1:
            edge = xmax
            vertical_diff[ymin, edge] += 1
            vertical_diff[ymax + 1, edge] -= 1
        if ymin > 0:
            edge = ymin - 1
            horizontal_diff[edge, xmin] += 1
            horizontal_diff[edge, xmax + 1] -= 1
        if ymax < ny - 1:
            edge = ymax
            horizontal_diff[edge, xmin] += 1
            horizontal_diff[edge, xmax + 1] -= 1

    vertical = np.cumsum(vertical_diff, axis=0)[:-1] > 0
    horizontal = np.cumsum(horizontal_diff, axis=1)[:, :-1] > 0

    dsu = _DSU(ny * nx)
    if nx > 1:
        allowed = (
            active_mask[:, :-1]
            & active_mask[:, 1:]
            & ~vertical
            & (mosaic[:, :-1] == mosaic[:, 1:])
            & (profile_grid[:, :-1] == profile_grid[:, 1:])
        )
        for y, x in zip(*np.where(allowed)):
            left = int(y) * nx + int(x)
            dsu.union(left, left + 1)
    if ny > 1:
        allowed = (
            active_mask[:-1, :]
            & active_mask[1:, :]
            & ~horizontal
            & (mosaic[:-1, :] == mosaic[1:, :])
            & (profile_grid[:-1, :] == profile_grid[1:, :])
        )
        for y, x in zip(*np.where(allowed)):
            top = int(y) * nx + int(x)
            dsu.union(top, top + nx)

    unit_grid = np.full((ny, nx), -1, dtype=np.int32)
    root_to_unit: dict[int, int] = {}
    representatives: list[tuple[int, int]] = []
    labels: list[int] = []
    profiles: list[int] = []
    for y_raw, x_raw in np.argwhere(active_mask):
        y, x = int(y_raw), int(x_raw)
        root = dsu.find(y * nx + x)
        unit = root_to_unit.get(root)
        if unit is None:
            unit = len(root_to_unit)
            root_to_unit[root] = unit
            representatives.append((y, x))
            labels.append(int(mosaic[y, x]))
            profiles.append(int(profile_grid[y, x]))
        unit_grid[y, x] = unit

    unit_count = len(root_to_unit)
    sizes = np.bincount(
        unit_grid[active_mask], minlength=unit_count
    ).astype(np.int32, copy=False)
    return {
        "grid": unit_grid,
        "sizes": sizes,
        "representatives": representatives,
        "labels": np.asarray(labels, dtype=np.int64),
        "profiles": np.asarray(profiles, dtype=np.int64),
        "input_elements": int(np.unique(mosaic[active_mask]).size),
        "elements": unit_count,
    }


def _full_units_in_rectangle(
    unit_grid: np.ndarray,
    unit_sizes: np.ndarray,
    xmin: int,
    ymin: int,
    xmax: int,
    ymax: int,
) -> np.ndarray:
    """Return units fully contained by a rectangle; refinement makes this exact."""

    block = unit_grid[ymin : ymax + 1, xmin : xmax + 1]
    values = block[block >= 0]
    if values.size == 0:
        return np.empty(0, dtype=np.int32)
    units, counts = np.unique(values, return_counts=True)
    if np.any(counts != unit_sizes[units]):  # defensive invariant check
        raise RuntimeError(
            "Внутренняя ошибка: mosaic-элемент пересечён границей прямоугольника"
        )
    return units.astype(np.int32, copy=False)


def _candidate_details(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": candidate["original_index"],
        "rectangle": candidate["rect"],
        "w": candidate["level"],
        "density": candidate["density"],
        "base_area": candidate.get("base_area", candidate["area"]),
        "area": candidate["area"],
        "hold": candidate.get("hold", 0.0),
        "hold_axis": candidate.get("hold_axis"),
        "cost": candidate["cost"],
    }


def _empty_result(
    *,
    status: str,
    n_requested: int | None,
    reason: str | None,
    stats: dict[str, Any],
    infeasibility_proved: bool = False,
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
        "n_requested": n_requested,
        "status": status,
        "is_feasible": False,
        "is_optimal": False,
        "solver_proved_optimal": False,
        "infeasibility_proved": infeasibility_proved,
        "termination_reason": "infeasible" if infeasibility_proved else "no_solution",
        "time_limit_reached": False,
        "has_incumbent": False,
        "incumbent_source": None,
        "reason": reason,
        "stats": stats,
        "component_statuses": [],
    }


def _greedy_start(
    candidates: list[dict[str, Any]],
    required_cells: Sequence[int],
    exact_count: int | None,
) -> set[int] | None:
    """Строит допустимый MIP-start; None означает, что жадно построить не удалось."""

    uncovered = set(map(int, required_cells))
    chosen: set[int] = set()

    while uncovered:
        best_index = None
        best_key = None

        for index, candidate in enumerate(candidates):
            if index in chosen:
                continue
            gain = sum(int(cell) in uncovered for cell in candidate["coverage"])
            if gain == 0:
                continue
            key = (candidate["cost"] / gain, candidate["cost"], -gain, index)
            if best_key is None or key < best_key:
                best_key = key
                best_index = index

        if best_index is None:
            return None

        chosen.add(best_index)
        uncovered.difference_update(map(int, candidates[best_index]["coverage"]))

    if exact_count is not None:
        if len(chosen) > exact_count:
            return None
        fillers = sorted(
            (index for index in range(len(candidates)) if index not in chosen),
            key=lambda index: (candidates[index]["cost"], index),
        )
        needed = exact_count - len(chosen)
        if needed > len(fillers):
            return None
        chosen.update(fillers[:needed])

    return chosen


def _verify_local_solution(
    candidates: list[dict[str, Any]],
    chosen_indices: Sequence[int],
    required_cells: Sequence[int],
    exact_count: int | None,
) -> bool:
    if exact_count is not None and len(chosen_indices) != exact_count:
        return False

    covered: set[int] = set()
    for index in chosen_indices:
        covered.update(map(int, candidates[index]["coverage"]))
    return all(int(cell) in covered for cell in required_cells)


def _solve_with_pulp(
    *,
    candidates: list[dict[str, Any]],
    required_cells: Sequence[int],
    exact_count: int | None,
    component_number: int,
    solver,
    solver_msg: bool,
    time_limit: float | None,
    threads: int | None,
    greedy_warm_start_limit: int,
    initial_original_indices: set[int] | None,
) -> dict[str, Any]:
    pulp = _import_pulp()

    model = pulp.LpProblem(
        f"minimum_density_rectangle_cover_{component_number}",
        pulp.LpMinimize,
    )
    variables = [
        pulp.LpVariable(f"selected_{component_number}_{index}", cat=pulp.LpBinary)
        for index in range(len(candidates))
    ]

    model += pulp.lpSum(
        candidate["cost"] * variables[index]
        for index, candidate in enumerate(candidates)
    )

    coverers: dict[int, list[int]] = {int(cell): [] for cell in required_cells}
    for candidate_index, candidate in enumerate(candidates):
        for cell in candidate["coverage"]:
            cell = int(cell)
            if cell in coverers:
                coverers[cell].append(candidate_index)

    for cell, candidate_indices in coverers.items():
        model += pulp.lpSum(variables[index] for index in candidate_indices) >= 1

    if exact_count is not None:
        model += pulp.lpSum(variables) == exact_count

    initial: set[int] | None = None
    if initial_original_indices:
        proposed = {
            index
            for index, candidate in enumerate(candidates)
            if candidate["original_index"] in initial_original_indices
        }
        if _verify_local_solution(candidates, proposed, required_cells, exact_count):
            initial = proposed

    if initial is None and len(candidates) <= greedy_warm_start_limit:
        initial = _greedy_start(candidates, required_cells, exact_count)

    warm_start = initial is not None
    if initial is not None:
        for index, variable in enumerate(variables):
            variable.setInitialValue(1 if index in initial else 0)

    active_solver = solver
    if active_solver is None:
        active_solver = _cbc_solver(
            msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            warm_start=warm_start,
            highs_options=highs_options,
        )

    solve_started = monotonic()
    warm_start_retry = False
    try:
        model.solve(active_solver)
    except pulp.PulpSolverError:
        if solver is not None or not warm_start:
            raise
        warm_start_retry = True
        elapsed_before_retry = monotonic() - solve_started
        remaining_limit = (
            None
            if time_limit is None
            else max(0.001, float(time_limit) - elapsed_before_retry)
        )
        active_solver = _cbc_solver(
            msg=solver_msg,
            time_limit=remaining_limit,
            threads=threads,
            warm_start=False,
            highs_options=highs_options,
        )
        model.solve(active_solver)
    solver_seconds = monotonic() - solve_started

    status_name = pulp.LpStatus.get(model.status, str(model.status))
    solution_code = getattr(model, "sol_status", None)
    solution_name = getattr(pulp, "LpSolution", {}).get(
        solution_code,
        str(solution_code),
    )
    optimal_solution_code = getattr(pulp, "LpSolutionOptimal", 1)
    integer_feasible_code = getattr(pulp, "LpSolutionIntegerFeasible", 2)
    proved_optimal = (
        model.status == pulp.LpStatusOptimal
        and (solution_code is None or solution_code == optimal_solution_code)
    )
    proved_infeasible = model.status == pulp.LpStatusInfeasible

    chosen_indices = [
        index
        for index, variable in enumerate(variables)
        if variable.varValue is not None and variable.varValue > 0.5
    ]
    feasible = _verify_local_solution(
        candidates,
        chosen_indices,
        required_cells,
        exact_count,
    )

    termination = _solver_termination_metadata(
        proved_optimal=proved_optimal,
        proved_infeasible=proved_infeasible,
        feasible=feasible,
        status=status_name,
        solution_status=solution_name,
        time_limit=time_limit,
        solver_seconds=solver_seconds,
        explicit_time_limit_reached=(
            True
            if solution_code == integer_feasible_code and time_limit is not None
            else None
        ),
    )
    return {
        "chosen_indices": chosen_indices,
        "status": status_name,
        "solution_status": solution_name,
        "solution_code": solution_code,
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        **termination,
        "objective": (
            float(sum(candidates[index]["cost"] for index in chosen_indices))
            if feasible
            else None
        ),
        "solver_seconds": float(solver_seconds),
        "mip_gap": None,
        "warm_start_used": warm_start,
        "warm_start_retry_without_start": warm_start_retry,
    }


def _solve_with_scipy(
    *,
    candidates: list[dict[str, Any]],
    required_cells: Sequence[int],
    exact_count: int | None,
    solver_msg: bool,
    time_limit: float | None,
) -> dict[str, Any]:
    """Резервный backend и средство тестирования; основной интерфейс остаётся PuLP."""

    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    cell_to_row = {int(cell): row for row, cell in enumerate(required_cells)}
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    lower: list[float] = []
    upper: list[float] = []

    for row, _cell in enumerate(required_cells):
        lower.append(1.0)
        upper.append(np.inf)

    for candidate_index, candidate in enumerate(candidates):
        for cell in candidate["coverage"]:
            row = cell_to_row.get(int(cell))
            if row is not None:
                rows.append(row)
                cols.append(candidate_index)
                data.append(1.0)

    if exact_count is not None:
        count_row = len(lower)
        for candidate_index in range(len(candidates)):
            rows.append(count_row)
            cols.append(candidate_index)
            data.append(1.0)
        lower.append(float(exact_count))
        upper.append(float(exact_count))

    matrix = coo_matrix(
        (data, (rows, cols)),
        shape=(len(lower), len(candidates)),
        dtype=float,
    ).tocsr()

    options: dict[str, Any] = {
        "disp": solver_msg,
        "mip_rel_gap": 0.0,
    }
    if time_limit is not None:
        options["time_limit"] = float(time_limit)

    solve_started = monotonic()
    result = milp(
        c=np.asarray([candidate["cost"] for candidate in candidates], dtype=float),
        integrality=np.ones(len(candidates), dtype=np.int8),
        bounds=Bounds(
            np.zeros(len(candidates), dtype=float),
            np.ones(len(candidates), dtype=float),
        ),
        constraints=LinearConstraint(
            matrix,
            np.asarray(lower, dtype=float),
            np.asarray(upper, dtype=float),
        ),
        options=options,
    )
    solver_seconds = monotonic() - solve_started

    status_map = {
        0: "Optimal",
        1: "Stopped",
        2: "Infeasible",
        3: "Unbounded",
        4: "Error",
    }
    status_name = status_map.get(int(result.status), str(result.status))
    proved_optimal = int(result.status) == 0
    proved_infeasible = int(result.status) == 2

    chosen_indices: list[int] = []
    if result.x is not None:
        chosen_indices = [
            int(index)
            for index, value in enumerate(result.x)
            if value > 0.5
        ]

    feasible = _verify_local_solution(
        candidates,
        chosen_indices,
        required_cells,
        exact_count,
    )

    termination = _solver_termination_metadata(
        proved_optimal=proved_optimal,
        proved_infeasible=proved_infeasible,
        feasible=feasible,
        status=status_name,
        solution_status=str(result.message),
        time_limit=time_limit,
        solver_seconds=solver_seconds,
        explicit_time_limit_reached=(int(result.status) == 1 and time_limit is not None),
    )
    return {
        "chosen_indices": chosen_indices,
        "status": status_name,
        "solution_status": str(result.message),
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        **termination,
        "objective": float(result.fun) if feasible and result.fun is not None else None,
        "solver_seconds": float(solver_seconds),
        "mip_gap": getattr(result, "mip_gap", None),
        "best_bound": getattr(result, "mip_dual_bound", None),
        "mip_node_count": getattr(result, "mip_node_count", None),
    }


def _solve_subproblem(
    *,
    candidates: list[dict[str, Any]],
    required_cells: Sequence[int],
    exact_count: int | None,
    component_number: int,
    backend: str,
    solver,
    solver_msg: bool,
    time_limit: float | None,
    threads: int | None,
    greedy_warm_start_limit: int,
    initial_original_indices: set[int] | None = None,
) -> dict[str, Any]:
    if backend == "pulp":
        return _solve_with_pulp(
            candidates=candidates,
            required_cells=required_cells,
            exact_count=exact_count,
            component_number=component_number,
            solver=solver,
            solver_msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            greedy_warm_start_limit=greedy_warm_start_limit,
            initial_original_indices=initial_original_indices,
        )
    if backend == "scipy":
        if solver is not None:
            raise ValueError("Параметр solver применим только к backend='pulp'")
        return _solve_with_scipy(
            candidates=candidates,
            required_cells=required_cells,
            exact_count=exact_count,
            solver_msg=solver_msg,
            time_limit=time_limit,
        )
    raise ValueError("backend должен быть равен 'pulp' или 'scipy'")


def _select_min_density_rectangles_legacy(
    value_matrix: Sequence[Sequence[int]],
    xs: Sequence[float],
    ys: Sequence[float],
    rectangles: Iterable[tuple[int, int, int, int, int]],
    densities: Mapping[int, float],
    n: int | None = None,
    *,
    holds: Mapping[int, float] | None = None,
    axis: str | int | None = None,
    mosaic: Sequence[Sequence[int]] | None = None,
    initial_indices: Sequence[int] | None = None,
    cover_zero_cells: bool = False,
    solver=None,
    solver_msg: bool = False,
    time_limit: float | None = None,
    threads: int | None = None,
    require_optimal: bool = True,
    decompose: bool = True,
    greedy_warm_start_limit: int = 2_000,
    backend: str = "pulp",
) -> dict[str, Any]:
    """
    Выбирает минимальный по стоимости набор прямоугольников-кандидатов.

    Параметры
    ---------
    value_matrix[y][x]
        Требуемый целочисленный уровень ячейки, 0..N.

    rectangles
        Кандидаты в формате ``(xmin, ymin, xmax, ymax, w)``.
        ``xmax`` и ``ymax`` включительные. Каждый элемент входного списка
        можно выбрать не более одного раза.

    densities[w]
        Положительная плотность класса ``w > 0``. Класс 0 необязателен;
        если он задан, его плотность может быть равна нулю. Ненулевые ключи
        должны идти подряд ``1, ..., N``, а значения — строго возрастать.

    n
        ``None``: количество прямоугольников выбирает оптимизатор.
        Целое число: выбирается СТРОГО ``n`` прямоугольников.

    Условия
    -------
    Для каждой требуемой ячейки должен быть выбран хотя бы один содержащий
    её прямоугольник с ``w >= value_matrix[y][x]``. Пересечения разрешены.

    Цель
    ----
    Минимизируется::

        sum(physical_area(rectangle) * densities[w])

    Остановка CBC по умолчанию
    --------------------------
    При ``time_limit=None`` явного лимита времени нет. При ``gapRel=0`` и
    ``gapAbs=0`` CBC продолжает поиск, пока не докажет оптимальность или
    недопустимость, либо пока процесс не будет внешне прерван/не завершится
    ошибкой. ``require_optimal`` не управляет остановкой: он только решает,
    выбрасывать ли исключение после solve, если оптимальность не доказана.
    """

    if n is not None:
        if isinstance(n, bool) or int(n) != n or int(n) < 0:
            raise ValueError("n должен быть неотрицательным целым или None")
        n = int(n)

    initial_index_set: set[int] | None = None
    if initial_indices is not None:
        if n is None:
            raise ValueError("initial_indices применим только при заданном n")
        normalized = []
        for index in initial_indices:
            if isinstance(index, bool) or int(index) != index:
                raise ValueError("initial_indices должен содержать целые индексы")
            normalized.append(int(index))
        initial_index_set = set(normalized)
        if len(initial_index_set) != n:
            raise ValueError("initial_indices должен содержать ровно n уникальных индексов")

    if time_limit is not None:
        time_limit = float(time_limit)
        if not np.isfinite(time_limit) or time_limit <= 0:
            raise ValueError("time_limit должен быть положительным или None")

    if threads is not None:
        if isinstance(threads, bool) or int(threads) != threads or int(threads) <= 0:
            raise ValueError("threads должен быть положительным целым или None")
        threads = int(threads)

    if greedy_warm_start_limit < 0:
        raise ValueError("greedy_warm_start_limit не может быть отрицательным")

    backend = str(backend).lower()
    if backend not in {"pulp", "scipy"}:
        raise ValueError("backend должен быть равен 'pulp' или 'scipy'")

    requirements = np.asarray(value_matrix)
    if requirements.ndim != 2:
        raise ValueError("value_matrix должна быть двумерной")
    if not np.all(np.isfinite(requirements)):
        raise ValueError("value_matrix содержит NaN или бесконечность")
    if not np.all(requirements == np.floor(requirements)):
        raise ValueError("value_matrix должна содержать целые значения")

    requirements = requirements.astype(np.int64, copy=False)
    if np.any(requirements < 0):
        raise ValueError("Значения value_matrix не могут быть отрицательными")

    ny, nx = requirements.shape
    normalized_mosaic = _normalize_mosaic(mosaic, (ny, nx))
    if len(xs) != nx or len(ys) != ny:
        raise ValueError("len(xs)/len(ys) не совпадают с размером матрицы")

    xs_array = np.asarray(xs, dtype=float)
    ys_array = np.asarray(ys, dtype=float)
    if (
        not np.all(np.isfinite(xs_array))
        or not np.all(np.isfinite(ys_array))
        or np.any(xs_array <= 0)
        or np.any(ys_array <= 0)
    ):
        raise ValueError("Все размеры xs и ys должны быть конечными и положительными")

    density: dict[int, float] = {}
    for key, value in densities.items():
        if isinstance(key, bool) or int(key) != key:
            raise ValueError("Ключи densities должны быть целыми")
        key = int(key)
        value = float(value)
        if (
            key < 0
            or not np.isfinite(value)
            or (key == 0 and value < 0)
            or (key > 0 and value <= 0)
        ):
            raise ValueError("Некорректный ключ или значение densities")
        density[key] = value

    if not density:
        raise ValueError("densities не должен быть пустым")
    # Class 0 is a zero-mass background by default. Explicit ``0: 0`` and an
    # omitted key are intentionally equivalent; a positive explicit value is
    # still respected for selectable class-0 rectangles.
    density.setdefault(0, 0.0)

    density_keys = sorted(density)
    nonzero_density_keys = [key for key in density_keys if key > 0]
    if nonzero_density_keys and nonzero_density_keys != list(
        range(1, nonzero_density_keys[-1] + 1)
    ):
        raise ValueError(
            "Ключи densities должны идти подряд среди ненулевых классов: 1, ..., N"
        )
    if any(
        density[left] >= density[right]
        for left, right in zip(density_keys, density_keys[1:])
    ):
        raise ValueError("Плотности должны строго возрастать с увеличением класса")

    normalized_holds, hold_axis = _normalize_holds_axis(density, holds, axis)

    max_required = int(requirements.max()) if requirements.size else 0
    if max_required > density_keys[-1]:
        raise ValueError(
            f"В матрице есть уровень {max_required}, но максимальный класс "
            f"densities равен {density_keys[-1]}"
        )

    x_edges = np.concatenate(([0.0], np.cumsum(xs_array)))
    y_edges = np.concatenate(([0.0], np.cumsum(ys_array)))

    raw_rectangles = list(rectangles)
    if initial_index_set is not None and any(
        index < 0 or index >= len(raw_rectangles) for index in initial_index_set
    ):
        raise ValueError("initial_indices содержит индекс вне rectangles")

    parsed_rectangles: list[dict[str, Any]] = []
    seen_rectangles: set[tuple[int, int, int, int, int]] = set()

    for original_index, raw in enumerate(raw_rectangles):
        if len(raw) != 5:
            raise ValueError(
                f"rectangles[{original_index}] должен иметь формат "
                "(xmin, ymin, xmax, ymax, w)"
            )
        if any(isinstance(value, bool) or int(value) != value for value in raw):
            raise ValueError(f"rectangles[{original_index}] должен содержать целые значения")

        xmin, ymin, xmax, ymax, level = map(int, raw)
        rect = (xmin, ymin, xmax, ymax, level)

        if not (0 <= xmin <= xmax < nx and 0 <= ymin <= ymax < ny):
            raise ValueError(
                f"Прямоугольник #{original_index} {rect} выходит за границы матрицы"
            )
        if level not in density:
            raise ValueError(f"Для класса w={level} нет плотности")

        # Без строгого n полные дубликаты никогда не нужны. При строгом n
        # они являются отдельными бинарными кандидатами и сохраняются.
        if n is None and rect in seen_rectangles:
            continue
        seen_rectangles.add(rect)

        base_area, area, hold = _rectangle_area_with_hold(
            x_edges,
            y_edges,
            xmin,
            ymin,
            xmax,
            ymax,
            level,
            normalized_holds,
            hold_axis,
        )
        parsed_rectangles.append(
            {
                "uid": original_index,
                "original_index": original_index,
                "rect": rect,
                "level": level,
                "density": density[level],
                "base_area": base_area,
                "area": area,
                "hold": hold,
                "hold_axis": hold_axis,
                "cost": float(area * density[level]),
            }
        )

    base_stats: dict[str, Any] = {
        "input_rectangles": len(raw_rectangles),
        "validated_candidates": len(parsed_rectangles),
        "unique_input_rectangles": len(seen_rectangles),
        "n_requested": n,
        "backend": backend,
        "holds_used": bool(normalized_holds),
        "hold_axis": hold_axis,
        "mosaic_used": normalized_mosaic is not None,
    }

    if n is not None and n > len(parsed_rectangles):
        return _empty_result(
            status="Infeasible",
            n_requested=n,
            reason=(
                f"Требуется выбрать n={n}, но доступно только "
                f"{len(parsed_rectangles)} кандидатов"
            ),
            stats=base_stats,
            infeasibility_proved=True,
        )

    active_mask = np.ones_like(requirements, dtype=bool) if cover_zero_cells else requirements > 0
    active_cell_count = int(active_mask.sum())
    active_count = active_cell_count
    base_stats["active_cells"] = active_cell_count

    # Если покрывать нечего, строгий n всё равно соблюдается: берутся n
    # самых дешёвых кандидатов. При n=None оптимально не выбирать ничего.
    if active_cell_count == 0:
        if n is None:
            selected_candidates: list[dict[str, Any]] = []
        else:
            selected_candidates = sorted(
                parsed_rectangles,
                key=lambda candidate: (candidate["cost"], candidate["original_index"]),
            )[:n]
            selected_candidates.sort(key=lambda candidate: candidate["original_index"])

        details = [_candidate_details(candidate) for candidate in selected_candidates]
        total_cost = float(sum(candidate["cost"] for candidate in selected_candidates))
        return {
            "rectangles": [candidate["rect"] for candidate in selected_candidates],
            "indices": [candidate["original_index"] for candidate in selected_candidates],
            "details": details,
            "total_cost": total_cost,
            "total_weighted_area": total_cost,
            "total_area": float(sum(candidate["area"] for candidate in selected_candidates)),
            "total_base_area": float(
                sum(candidate["base_area"] for candidate in selected_candidates)
            ),
            "n_rectangles": len(selected_candidates),
            "n_requested": n,
            "status": "Optimal",
            "is_feasible": True,
            "is_optimal": True,
            "solver_proved_optimal": True,
            "infeasibility_proved": False,
            "reason": None,
            "stats": {
                **base_stats,
                "usable_candidates": len(parsed_rectangles),
                "forced_candidates": 0,
                "components": 0,
                "decomposition_used": False,
                "candidate_cell_incidence": 0,
                "constraints_before_mosaic": 0,
                "constraints_after_mosaic": 0,
                "mosaic_input_elements": 0,
                "mosaic_elements": 0,
            },
            "component_statuses": [],
        }

    if n == 0:
        return _empty_result(
            status="Infeasible",
            n_requested=n,
            reason="Ненулевая матрица не может быть покрыта нулём прямоугольников",
            stats=base_stats,
            infeasibility_proved=True,
        )

    # Для n=1 MILP не нужен: перебираем только кандидаты, которые содержат
    # все активные ячейки и имеют достаточный класс, затем берём минимальную стоимость.
    if n == 1:
        yy, xx = np.nonzero(active_mask)
        xmin_req, xmax_req = int(xx.min()), int(xx.max())
        ymin_req, ymax_req = int(yy.min()), int(yy.max())
        level_req = int(requirements[active_mask].max())
        feasible = [
            c for c in parsed_rectangles
            if c["rect"][0] <= xmin_req <= xmax_req <= c["rect"][2]
            and c["rect"][1] <= ymin_req <= ymax_req <= c["rect"][3]
            and c["level"] >= level_req
        ]
        if not feasible:
            return _empty_result(
                status="Infeasible", n_requested=1,
                reason="Нет одного прямоугольника, покрывающего все требуемые ячейки",
                stats={**base_stats, "n1_fast_path": True},
                infeasibility_proved=True,
            )
        chosen = min(feasible, key=lambda c: (c["cost"], c["original_index"]))
        return {
            "rectangles": [chosen["rect"]],
            "indices": [chosen["original_index"]],
            "details": [_candidate_details(chosen)],
            "total_cost": chosen["cost"],
            "total_weighted_area": chosen["cost"],
            "total_area": chosen["area"],
            "total_base_area": chosen["base_area"],
            "n_rectangles": 1,
            "n_requested": 1,
            "status": "Optimal",
            "is_feasible": True,
            "is_optimal": True,
            "solver_proved_optimal": True,
            "infeasibility_proved": False,
            "reason": None,
            "stats": {
                **base_stats,
                "usable_candidates": len(feasible),
                "forced_candidates": 1,
                "components": 0,
                "decomposition_used": False,
                "candidate_cell_incidence": 0,
                "strict_count_model": True,
                "n1_fast_path": True,
            },
            "component_statuses": [],
        }

    unit_representatives = [tuple(map(int, yx)) for yx in np.argwhere(active_mask)]
    unit_requirements = requirements[active_mask].astype(np.int64, copy=False)
    mosaic_layout: dict[str, Any] | None = None
    if normalized_mosaic is None:
        cell_id = np.full((ny, nx), -1, dtype=np.int32)
        cell_id[active_mask] = np.arange(active_cell_count, dtype=np.int32)
        base_stats.update(
            {
                "constraints_before_mosaic": active_cell_count,
                "constraints_after_mosaic": active_cell_count,
                "mosaic_input_elements": active_cell_count,
                "mosaic_elements": active_cell_count,
            }
        )
    else:
        profile_grid = requirements.astype(np.int64, copy=False)
        mosaic_layout = _build_mosaic_units(
            active_mask=active_mask,
            mosaic=normalized_mosaic,
            profile_grid=profile_grid,
            candidates=parsed_rectangles,
        )
        cell_id = mosaic_layout["grid"]
        active_count = int(mosaic_layout["elements"])
        unit_representatives = list(mosaic_layout["representatives"])
        unit_requirements = np.asarray(
            [requirements[y, x] for y, x in unit_representatives], dtype=np.int64
        )
        base_stats.update(
            {
                "constraints_before_mosaic": active_cell_count,
                "constraints_after_mosaic": active_count,
                "mosaic_input_elements": int(mosaic_layout["input_elements"]),
                "mosaic_elements": active_count,
                "mosaic_refinement_splits": (
                    active_count - int(mosaic_layout["input_elements"])
                ),
            }
        )

    levels_present = {candidate["level"] for candidate in parsed_rectangles}
    eligibility_prefix: dict[int, np.ndarray] = {}
    if normalized_mosaic is None:
        for level in levels_present:
            eligible = active_mask & (requirements <= level)
            prefix = np.zeros((ny + 1, nx + 1), dtype=np.int64)
            prefix[1:, 1:] = eligible.cumsum(axis=0).cumsum(axis=1)
            eligibility_prefix[level] = prefix

    def eligible_count(candidate: dict[str, Any]) -> int:
        xmin, ymin, xmax, ymax, level = candidate["rect"]
        prefix = eligibility_prefix[level]
        return int(
            prefix[ymax + 1, xmax + 1]
            - prefix[ymin, xmax + 1]
            - prefix[ymax + 1, xmin]
            + prefix[ymin, xmin]
        )

    coverage_groups: dict[bytes, list[dict[str, Any]]] = defaultdict(list)

    for candidate in parsed_rectangles:
        xmin, ymin, xmax, ymax, level = candidate["rect"]

        if mosaic_layout is not None:
            geometric_units = _full_units_in_rectangle(
                cell_id,
                mosaic_layout["sizes"],
                xmin,
                ymin,
                xmax,
                ymax,
            )
            coverage = geometric_units[
                unit_requirements[geometric_units] <= level
            ].astype(np.int32, copy=False)
        elif eligible_count(candidate) == 0:
            coverage = np.empty(0, dtype=np.int32)
        else:
            ids = cell_id[ymin : ymax + 1, xmin : xmax + 1]
            values = requirements[ymin : ymax + 1, xmin : xmax + 1]
            coverage = ids[(ids >= 0) & (values <= level)].astype(np.int32, copy=True)

        # При n=None кандидат без полезного покрытия всегда только увеличивает
        # положительную целевую функцию. При строгом n он может быть filler.
        if coverage.size == 0 and n is None:
            continue

        enriched = dict(candidate)
        enriched["full_coverage"] = coverage
        enriched["coverage"] = coverage
        coverage_groups[coverage.tobytes()].append(enriched)

    if initial_index_set is not None:
        initial_candidates = [
            candidate
            for group in coverage_groups.values()
            for candidate in group
            if candidate["original_index"] in initial_index_set
        ]
        covered_by_start = np.zeros(active_count, dtype=bool)
        for candidate in initial_candidates:
            covered_by_start[candidate["full_coverage"]] = True
        if len(initial_candidates) != n or not np.all(covered_by_start):
            raise ValueError("initial_indices не задаёт допустимое покрытие для данного n")

    candidates: list[dict[str, Any]] = []
    for group in coverage_groups.values():
        group.sort(key=lambda candidate: (candidate["cost"], candidate["original_index"]))
        if n is None:
            candidates.append(group[0])
        else:
            # Сохраняем обычный точный presolve и дополнительно не удаляем
            # кандидаты MIP-start. Это не меняет оптимум: добавляются лишь
            # варианты с тем же покрытием, которые presolve считал доминируемыми.
            kept = group[: min(n, len(group))]
            if initial_index_set:
                seen = {candidate["uid"] for candidate in kept}
                kept += [
                    candidate for candidate in group
                    if candidate["original_index"] in initial_index_set
                    and candidate["uid"] not in seen
                ]
            candidates.extend(kept)

    candidates.sort(key=lambda candidate: candidate["original_index"])
    base_stats["usable_candidates"] = len(candidates)
    base_stats["candidate_cell_incidence"] = int(
        sum(len(candidate["full_coverage"]) for candidate in candidates)
    )

    if n is not None and n > len(candidates):
        return _empty_result(
            status="Infeasible",
            n_requested=n,
            reason=(
                "После точного удаления доминируемых вариантов осталось "
                f"{len(candidates)} кандидатов, меньше требуемого n={n}"
            ),
            stats=base_stats,
            infeasibility_proved=True,
        )

    coverage_count = np.zeros(active_count, dtype=np.int32)
    for candidate in candidates:
        coverage_count[candidate["full_coverage"]] += 1

    missing = np.flatnonzero(coverage_count == 0)
    if missing.size:
        sample = [unit_representatives[int(cell)] for cell in missing[:10]]
        return _empty_result(
            status="Infeasible",
            n_requested=n,
            reason=(
                "Некоторые требуемые ячейки не покрываются ни одним "
                f"допустимым прямоугольником. Первые координаты (y, x): {sample}"
            ),
            stats=base_stats,
            infeasibility_proved=True,
        )

    # ------------------------------------------------------------------
    # Точный presolve. Для n=None можно удалять все уже бесполезные
    # кандидаты. Для строгого n они сохраняются как возможные fillers.
    # ------------------------------------------------------------------
    unsatisfied = np.ones(active_count, dtype=bool)
    forced: list[dict[str, Any]] = []
    working = list(candidates)
    remaining_slots = None if n is None else n

    while True:
        if not np.any(unsatisfied):
            if remaining_slots is None:
                working = []
            else:
                working = sorted(
                    working,
                    key=lambda candidate: (candidate["cost"], candidate["original_index"]),
                )[:remaining_slots]
                for candidate in working:
                    candidate["coverage"] = np.empty(0, dtype=np.int32)
            break

        if remaining_slots == 0:
            return _empty_result(
                status="Infeasible",
                n_requested=n,
                reason="Обязательные кандидаты исчерпали n, но часть ячеек не покрыта",
                stats={**base_stats, "forced_candidates": len(forced)},
                infeasibility_proved=True,
            )

        projected_groups: dict[bytes, list[dict[str, Any]]] = defaultdict(list)
        for candidate in working:
            full = candidate["full_coverage"]
            coverage = full[unsatisfied[full]] if full.size else full
            if coverage.size == 0 and n is None:
                continue

            projected = dict(candidate)
            projected["coverage"] = coverage
            projected_groups[coverage.tobytes()].append(projected)

        reduced: list[dict[str, Any]] = []
        for group in projected_groups.values():
            group.sort(key=lambda candidate: (candidate["cost"], candidate["original_index"]))
            if remaining_slots is None:
                reduced.append(group[0])
            else:
                kept = group[: min(remaining_slots, len(group))]
                if initial_index_set:
                    seen = {candidate["uid"] for candidate in kept}
                    kept += [
                        candidate for candidate in group
                        if candidate["original_index"] in initial_index_set
                        and candidate["uid"] not in seen
                    ]
                reduced.extend(kept)
        working = reduced

        if remaining_slots is not None and remaining_slots > len(working):
            return _empty_result(
                status="Infeasible",
                n_requested=n,
                reason=(
                    f"После presolve осталось {len(working)} свободных кандидатов, "
                    f"но нужно выбрать ещё {remaining_slots}"
                ),
                stats={**base_stats, "forced_candidates": len(forced)},
                infeasibility_proved=True,
            )

        counts = np.zeros(active_count, dtype=np.int32)
        last_candidate = np.full(active_count, -1, dtype=np.int32)
        for index, candidate in enumerate(working):
            if candidate["coverage"].size:
                counts[candidate["coverage"]] += 1
                last_candidate[candidate["coverage"]] = index

        remaining_cells = np.flatnonzero(unsatisfied)
        if np.any(counts[remaining_cells] == 0):
            return _empty_result(
                status="Infeasible",
                n_requested=n,
                reason="После presolve потеряно допустимое покрытие",
                stats={**base_stats, "forced_candidates": len(forced)},
                infeasibility_proved=True,
            )

        singleton_cells = remaining_cells[counts[remaining_cells] == 1]
        if singleton_cells.size == 0:
            break

        forced_indices = sorted(set(map(int, np.unique(last_candidate[singleton_cells]))))
        new_forced = [working[index] for index in forced_indices]

        if remaining_slots is not None and len(new_forced) > remaining_slots:
            return _empty_result(
                status="Infeasible",
                n_requested=n,
                reason="Число обязательных прямоугольников превышает n",
                stats={**base_stats, "forced_candidates": len(forced)},
                infeasibility_proved=True,
            )

        forced.extend(new_forced)
        for candidate in new_forced:
            unsatisfied[candidate["full_coverage"]] = False

        forced_uids = {candidate["uid"] for candidate in new_forced}
        working = [candidate for candidate in working if candidate["uid"] not in forced_uids]
        if remaining_slots is not None:
            remaining_slots -= len(new_forced)

    selected_candidates = list(forced)
    component_statuses: list[dict[str, Any]] = []
    solve_started = monotonic()

    def remaining_time() -> float | None:
        if time_limit is None or solver is not None:
            return time_limit
        remaining = time_limit - (monotonic() - solve_started)
        return max(0.001, remaining)

    if np.any(unsatisfied):
        remaining_cells = np.flatnonzero(unsatisfied)

        if n is not None:
            # Строгое количество создаёт одну глобальную связывающую строку,
            # поэтому независимые пятна нельзя решать отдельно без отдельного
            # DP по количествам. Один глобальный MILP здесь и проще, и точнее.
            assert remaining_slots is not None
            if remaining_slots > len(working):
                return _empty_result(
                    status="Infeasible",
                    n_requested=n,
                    reason=(
                        f"Нужно выбрать ещё {remaining_slots} кандидатов, "
                        f"а осталось только {len(working)}"
                    ),
                    stats={**base_stats, "forced_candidates": len(forced)},
                    infeasibility_proved=True,
                )

            subresult = _solve_subproblem(
                candidates=working,
                required_cells=remaining_cells,
                exact_count=remaining_slots,
                component_number=0,
                backend=backend,
                solver=solver,
                solver_msg=solver_msg,
                time_limit=remaining_time(),
                threads=threads,
                greedy_warm_start_limit=greedy_warm_start_limit,
                initial_original_indices=initial_index_set,
            )

            if not subresult["feasible"]:
                if require_optimal and not subresult["proved_infeasible"]:
                    raise RuntimeError(
                        "Решатель завершился без допустимого решения и без "
                        f"доказательства недопустимости для n={n}; "
                        f"status={subresult['status']}, "
                        f"solution_status={subresult['solution_status']}"
                    )
                status = "Infeasible" if subresult["proved_infeasible"] else subresult["status"]
                result = _empty_result(
                    status=status,
                    n_requested=n,
                    reason=(
                        "Допустимое решение со строгим количеством "
                        f"n={n} не найдено; solver_status={subresult['status']}, "
                        f"solution_status={subresult['solution_status']}"
                    ),
                    stats={
                        **base_stats,
                        "forced_candidates": len(forced),
                        "components": 1,
                        "decomposition_used": False,
                    },
                    infeasibility_proved=subresult["proved_infeasible"],
                )
                result.update(
                    {
                        "termination_reason": subresult.get(
                            "termination_reason",
                            "infeasible" if subresult["proved_infeasible"] else "no_solution",
                        ),
                        "time_limit_reached": bool(
                            subresult.get("time_limit_reached", False)
                        ),
                    }
                )
                result["stats"].update(
                    {
                        "solver_seconds": float(subresult.get("solver_seconds", 0.0)),
                        "mip_gap": subresult.get("mip_gap"),
                        "best_bound": subresult.get("best_bound"),
                        "termination_reason": result["termination_reason"],
                        "time_limit_reached": result["time_limit_reached"],
                    }
                )
                return result

            if require_optimal and not subresult["proved_optimal"]:
                raise RuntimeError(
                    f"Оптимум для n={n} не доказан; "
                    f"status={subresult['status']}, "
                    f"solution_status={subresult['solution_status']}"
                )

            selected_candidates.extend(
                working[index] for index in subresult["chosen_indices"]
            )
            component_statuses.append(
                {
                    **subresult,
                    "cells": len(remaining_cells),
                    "candidates": len(working),
                    "exact_count": remaining_slots,
                }
            )
            components_count = 1
            decomposition_used = False

        else:
            # Без ограничения на количество задача точно распадается по
            # компонентам гиперграфа «ячейка — покрывающий кандидат».
            if decompose:
                compact_id = np.full(active_count, -1, dtype=np.int32)
                compact_id[remaining_cells] = np.arange(remaining_cells.size, dtype=np.int32)
                dsu = _DSU(int(remaining_cells.size))

                for candidate in working:
                    local = compact_id[candidate["coverage"]]
                    first = int(local[0])
                    for other in local[1:]:
                        dsu.union(first, int(other))

                cells_by_root: dict[int, list[int]] = defaultdict(list)
                for global_cell in remaining_cells:
                    root = dsu.find(int(compact_id[global_cell]))
                    cells_by_root[root].append(int(global_cell))

                candidates_by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for candidate in working:
                    root = dsu.find(int(compact_id[candidate["coverage"][0]]))
                    candidates_by_root[root].append(candidate)

                components = [
                    (cells_by_root[root], candidates_by_root[root])
                    for root in cells_by_root
                ]
            else:
                components = [(list(map(int, remaining_cells)), working)]

            for component_number, (component_cells, component_candidates) in enumerate(components):
                if len(component_cells) == 1:
                    chosen = min(
                        component_candidates,
                        key=lambda item: (item["cost"], item["original_index"]),
                    )
                    selected_candidates.append(chosen)
                    component_statuses.append(
                        {
                            "status": "Optimal",
                            "solution_status": "trivial",
                            "proved_optimal": True,
                            "proved_infeasible": False,
                            "feasible": True,
                            "cells": 1,
                            "candidates": len(component_candidates),
                        }
                    )
                    continue

                subresult = _solve_subproblem(
                    candidates=component_candidates,
                    required_cells=component_cells,
                    exact_count=None,
                    component_number=component_number,
                    backend=backend,
                    solver=solver,
                    solver_msg=solver_msg,
                    time_limit=remaining_time(),
                    threads=threads,
                    greedy_warm_start_limit=greedy_warm_start_limit,
                )

                if not subresult["feasible"]:
                    if require_optimal and not subresult["proved_infeasible"]:
                        raise RuntimeError(
                            "Решатель завершился без допустимого решения и без "
                            f"доказательства недопустимости для компоненты "
                            f"{component_number}; status={subresult['status']}, "
                            f"solution_status={subresult['solution_status']}"
                        )
                    status = "Infeasible" if subresult["proved_infeasible"] else subresult["status"]
                    return _empty_result(
                        status=status,
                        n_requested=n,
                        reason=(
                            f"Для компоненты {component_number} решение не найдено; "
                            f"status={subresult['status']}, "
                            f"solution_status={subresult['solution_status']}"
                        ),
                        stats={
                            **base_stats,
                            "forced_candidates": len(forced),
                            "components": len(components),
                            "decomposition_used": decompose,
                        },
                        infeasibility_proved=subresult["proved_infeasible"],
                    )

                if require_optimal and not subresult["proved_optimal"]:
                    raise RuntimeError(
                        f"Оптимум для компоненты {component_number} не доказан; "
                        f"status={subresult['status']}, "
                        f"solution_status={subresult['solution_status']}"
                    )

                selected_candidates.extend(
                    component_candidates[index]
                    for index in subresult["chosen_indices"]
                )
                component_statuses.append(
                    {
                        **subresult,
                        "cells": len(component_cells),
                        "candidates": len(component_candidates),
                    }
                )

            components_count = len(components)
            decomposition_used = decompose
    else:
        # Все ячейки закрылись обязательными кандидатами. При строгом n
        # добираем самые дешёвые оставшиеся варианты до точного количества.
        if n is not None:
            assert remaining_slots is not None
            fillers = sorted(
                working,
                key=lambda candidate: (candidate["cost"], candidate["original_index"]),
            )
            if remaining_slots > len(fillers):
                return _empty_result(
                    status="Infeasible",
                    n_requested=n,
                    reason="Недостаточно кандидатов, чтобы добрать строгое n",
                    stats={**base_stats, "forced_candidates": len(forced)},
                    infeasibility_proved=True,
                )
            selected_candidates.extend(fillers[:remaining_slots])
        components_count = 0
        decomposition_used = False

    # ------------------------------------------------------------------
    # Защитная проверка итогового решения.
    # ------------------------------------------------------------------
    covered = np.zeros(active_count, dtype=bool)
    for candidate in selected_candidates:
        covered[candidate["full_coverage"]] = True

    if not np.all(covered):
        raise RuntimeError("Внутренняя ошибка: итоговое решение не покрывает все требуемые ячейки")
    if n is not None and len(selected_candidates) != n:
        raise RuntimeError(
            f"Внутренняя ошибка: выбрано {len(selected_candidates)} прямоугольников вместо n={n}"
        )

    selected_candidates.sort(key=lambda candidate: candidate["original_index"])
    all_components_optimal = all(
        item.get("proved_optimal", True) for item in component_statuses
    )
    any_time_limit = any(
        bool(item.get("time_limit_reached", False)) for item in component_statuses
    )
    termination_reason = (
        "optimal"
        if all_components_optimal
        else "time_limit"
        if any_time_limit
        else "solver_stopped"
    )

    details = [_candidate_details(candidate) for candidate in selected_candidates]
    total_cost = float(sum(candidate["cost"] for candidate in selected_candidates))

    return {
        "rectangles": [candidate["rect"] for candidate in selected_candidates],
        "indices": [candidate["original_index"] for candidate in selected_candidates],
        "details": details,
        "total_cost": total_cost,
        "total_weighted_area": total_cost,
        "total_area": float(sum(candidate["area"] for candidate in selected_candidates)),
        "total_base_area": float(
            sum(candidate["base_area"] for candidate in selected_candidates)
        ),
        "n_rectangles": len(selected_candidates),
        "n_requested": n,
        "status": "Optimal" if all_components_optimal else "Feasible",
        "is_feasible": True,
        "is_optimal": all_components_optimal,
        "solver_proved_optimal": all_components_optimal,
        "infeasibility_proved": False,
        "termination_reason": termination_reason,
        "time_limit_reached": any_time_limit,
        "has_incumbent": True,
        "incumbent_source": "solver",
        "reason": None,
        "stats": {
            **base_stats,
            "forced_candidates": len(forced),
            "components": components_count,
            "decomposition_used": decomposition_used,
            "strict_count_model": n is not None,
            "termination_reason": termination_reason,
            "time_limit_reached": any_time_limit,
        },
        "component_statuses": component_statuses,
    }
# =============================================================================
# Recipe-aware model. The legacy path above is called verbatim when recipes is
# empty, so no recipe rows or variables are created for the original problem.
# =============================================================================


def _normalize_recipes(
    recipes: Mapping[int, Sequence[int]],
    density: Mapping[int, float],
) -> dict[int, tuple[int, ...]]:
    normalized: dict[int, tuple[int, ...]] = {}
    for raw_class, raw_layers in recipes.items():
        if isinstance(raw_class, bool) or int(raw_class) != raw_class:
            raise ValueError("Ключи recipes должны быть целыми")
        class_id = int(raw_class)
        if class_id < 0:
            raise ValueError("Ключи recipes не могут быть отрицательными")

        layers: list[int] = []
        for raw_layer in raw_layers:
            if isinstance(raw_layer, bool) or int(raw_layer) != raw_layer:
                raise ValueError(f"recipes[{class_id}] должен содержать целые классы")
            layer = int(raw_layer)
            if layer != 0 and layer not in density:
                raise ValueError(
                    f"recipes[{class_id}] содержит класс {layer}, "
                    "для которого нет densities"
                )
            layers.append(layer)

        if not layers:
            raise ValueError(f"recipes[{class_id}] не может быть пустым")
        normalized[class_id] = tuple(sorted(layers))

    return normalized


def _recipe_thresholds(layers: Sequence[int]) -> tuple[tuple[int, int], ...]:
    """(threshold, required_count) for the injection rule level >= layer."""
    ordered = tuple(sorted(map(int, layers)))
    return tuple(
        (threshold, sum(layer >= threshold for layer in ordered))
        for threshold in sorted(set(ordered))
    )


def _class_layers(
    class_id: int,
    density: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]],
) -> tuple[int, ...]:
    """Primitive layer profile of a requirement or rectangle class."""
    if class_id in recipes:
        return tuple(map(int, recipes[class_id]))
    if class_id == 0:
        return (0,)
    if class_id in density:
        return (int(class_id),)
    raise ValueError(f"Для класса {class_id} нет ни density, ни recipes")


def _verify_recipe_local_solution(
    candidates: Sequence[dict[str, Any]],
    chosen_indices: Sequence[int],
    row_needs: np.ndarray,
    exact_count: int | None,
) -> bool:
    if exact_count is not None and len(chosen_indices) != exact_count:
        return False

    counts = np.zeros(len(candidates), dtype=np.int32)
    for raw_index in chosen_indices:
        index = int(raw_index)
        if index < 0 or index >= len(candidates):
            return False
        counts[index] += 1
        if counts[index] > int(candidates[index]["upper_bound"]):
            return False

    supplied = np.zeros(len(row_needs), dtype=np.int32)
    for index, count_raw in enumerate(counts):
        count = int(count_raw)
        if count == 0:
            continue
        candidate = candidates[index]
        rows = candidate["rows"]
        if rows.size:
            supplied[rows] += candidate["coefficients"] * count
    return bool(np.all(supplied >= row_needs))


def _greedy_recipe_start(
    candidates: list[dict[str, Any]],
    row_needs: np.ndarray,
    exact_count: int | None,
) -> list[int] | None:
    residual = row_needs.astype(np.int32, copy=True)
    counts = np.zeros(len(candidates), dtype=np.int32)

    while np.any(residual > 0):
        if exact_count is not None and int(counts.sum()) >= exact_count:
            return None
        best_index: int | None = None
        best_key: tuple[float, float, int, int] | None = None
        for index, candidate in enumerate(candidates):
            if counts[index] >= int(candidate["upper_bound"]):
                continue
            rows = candidate["rows"]
            coefficients = candidate["coefficients"]
            gain = (
                int(np.minimum(residual[rows], coefficients).sum())
                if rows.size
                else 0
            )
            if gain == 0:
                continue
            key = (candidate["cost"] / gain, candidate["cost"], -gain, index)
            if best_key is None or key < best_key:
                best_key = key
                best_index = index

        if best_index is None:
            return None

        counts[best_index] += 1
        candidate = candidates[best_index]
        rows = candidate["rows"]
        residual[rows] = np.maximum(
            0, residual[rows] - candidate["coefficients"]
        )

    if exact_count is not None:
        needed = exact_count - int(counts.sum())
        while needed > 0:
            fillers = [
                index
                for index, candidate in enumerate(candidates)
                if counts[index] < int(candidate["upper_bound"])
            ]
            if not fillers:
                return None
            best = min(
                fillers,
                key=lambda index: (candidates[index]["cost"], index),
            )
            add = min(
                needed,
                int(candidates[best]["upper_bound"]) - int(counts[best]),
            )
            counts[best] += add
            needed -= add

    return [
        index
        for index, count_raw in enumerate(counts)
        for _ in range(int(count_raw))
    ]


def _multiplicity_details(
    candidates: Sequence[dict[str, Any]],
    chosen_indices: Sequence[int],
) -> list[dict[str, Any]]:
    counts = Counter(int(index) for index in chosen_indices)
    return [
        {
            "index": candidates[index]["original_index"],
            "rectangle": candidates[index]["rect"],
            "count": int(count),
        }
        for index, count in sorted(
            counts.items(),
            key=lambda item: candidates[item[0]]["original_index"],
        )
    ]


def _solve_recipe_with_pulp(
    *,
    candidates: list[dict[str, Any]],
    row_needs: np.ndarray,
    exact_count: int | None,
    solver,
    solver_msg: bool,
    time_limit: float | None,
    threads: int | None,
    greedy_warm_start_limit: int,
    initial_local_indices: Sequence[int] | None,
) -> dict[str, Any]:
    pulp = _import_pulp()

    model = pulp.LpProblem("minimum_density_rectangle_cover_recipes", pulp.LpMinimize)
    variables = [
        pulp.LpVariable(
            f"selected_recipe_{index}",
            lowBound=0,
            upBound=int(candidate["upper_bound"]),
            cat=pulp.LpInteger,
        )
        for index, candidate in enumerate(candidates)
    ]
    model += pulp.lpSum(
        candidate["cost"] * variables[index]
        for index, candidate in enumerate(candidates)
    )

    coverers: list[list[tuple[int, int]]] = [[] for _ in range(len(row_needs))]
    for candidate_index, candidate in enumerate(candidates):
        for row, coefficient in zip(candidate["rows"], candidate["coefficients"]):
            coverers[int(row)].append((candidate_index, int(coefficient)))

    for row, candidate_terms in enumerate(coverers):
        model += (
            pulp.lpSum(
                coefficient * variables[index]
                for index, coefficient in candidate_terms
            )
            >= int(row_needs[row])
        )

    if exact_count is not None:
        model += pulp.lpSum(variables) == exact_count

    initial: list[int] | None = None
    if initial_local_indices is not None and _verify_recipe_local_solution(
        candidates, initial_local_indices, row_needs, exact_count
    ):
        initial = list(map(int, initial_local_indices))

    if initial is None and len(candidates) <= greedy_warm_start_limit:
        initial = _greedy_recipe_start(candidates, row_needs, exact_count)

    warm_start = initial is not None
    if initial is not None:
        initial_counts = Counter(initial)
        for index, variable in enumerate(variables):
            variable.setInitialValue(int(initial_counts.get(index, 0)))

    active_solver = solver
    if active_solver is None:
        active_solver = _cbc_solver(
            msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            warm_start=warm_start,
            highs_options=highs_options,
        )

    solve_started = monotonic()
    warm_start_retry = False
    try:
        model.solve(active_solver)
    except pulp.PulpSolverError:
        if solver is not None or not warm_start:
            raise
        warm_start_retry = True
        elapsed_before_retry = monotonic() - solve_started
        remaining_limit = (
            None
            if time_limit is None
            else max(0.001, float(time_limit) - elapsed_before_retry)
        )
        active_solver = _cbc_solver(
            msg=solver_msg,
            time_limit=remaining_limit,
            threads=threads,
            warm_start=False,
        )
        model.solve(active_solver)
    solver_seconds = monotonic() - solve_started

    status_name = pulp.LpStatus.get(model.status, str(model.status))
    solution_code = getattr(model, "sol_status", None)
    solution_name = getattr(pulp, "LpSolution", {}).get(solution_code, str(solution_code))
    optimal_solution_code = getattr(pulp, "LpSolutionOptimal", 1)
    integer_feasible_code = getattr(pulp, "LpSolutionIntegerFeasible", 2)
    proved_optimal = (
        model.status == pulp.LpStatusOptimal
        and (solution_code is None or solution_code == optimal_solution_code)
    )
    proved_infeasible = model.status == pulp.LpStatusInfeasible

    chosen_indices: list[int] = []
    chosen_counts: list[int] = []
    for index, variable in enumerate(variables):
        value = 0 if variable.varValue is None else int(round(float(variable.varValue)))
        value = max(0, min(value, int(candidates[index]["upper_bound"])))
        chosen_counts.append(value)
        chosen_indices.extend([index] * value)

    feasible = _verify_recipe_local_solution(
        candidates, chosen_indices, row_needs, exact_count
    )

    termination = _solver_termination_metadata(
        proved_optimal=proved_optimal,
        proved_infeasible=proved_infeasible,
        feasible=feasible,
        status=status_name,
        solution_status=solution_name,
        time_limit=time_limit,
        solver_seconds=solver_seconds,
        explicit_time_limit_reached=(
            True
            if solution_code == integer_feasible_code and time_limit is not None
            else None
        ),
    )
    return {
        "chosen_indices": chosen_indices,
        "chosen_counts": chosen_counts,
        "status": status_name,
        "solution_status": solution_name,
        "solution_code": solution_code,
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        **termination,
        "objective": (
            float(sum(candidates[index]["cost"] for index in chosen_indices))
            if feasible
            else None
        ),
        "solver_seconds": float(solver_seconds),
        "mip_gap": None,
        "warm_start_used": warm_start,
        "warm_start_retry_without_start": warm_start_retry,
    }


def _solve_recipe_with_scipy(
    *,
    candidates: list[dict[str, Any]],
    row_needs: np.ndarray,
    exact_count: int | None,
    solver_msg: bool,
    time_limit: float | None,
) -> dict[str, Any]:
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    rows: list[int] = []
    cols: list[int] = []
    values: list[float] = []
    lower = row_needs.astype(float).tolist()
    upper = [np.inf] * len(row_needs)

    for candidate_index, candidate in enumerate(candidates):
        for row, coefficient in zip(candidate["rows"], candidate["coefficients"]):
            rows.append(int(row))
            cols.append(candidate_index)
            values.append(float(coefficient))

    if exact_count is not None:
        count_row = len(lower)
        rows.extend([count_row] * len(candidates))
        cols.extend(range(len(candidates)))
        values.extend([1.0] * len(candidates))
        lower.append(float(exact_count))
        upper.append(float(exact_count))

    matrix = coo_matrix(
        (values, (rows, cols)),
        shape=(len(lower), len(candidates)),
        dtype=float,
    ).tocsr()

    options: dict[str, Any] = {"disp": solver_msg, "mip_rel_gap": 0.0}
    if time_limit is not None:
        options["time_limit"] = float(time_limit)

    solve_started = monotonic()
    result = milp(
        c=np.asarray([candidate["cost"] for candidate in candidates], dtype=float),
        integrality=np.ones(len(candidates), dtype=np.int8),
        bounds=Bounds(
            np.zeros(len(candidates), dtype=float),
            np.asarray(
                [candidate["upper_bound"] for candidate in candidates],
                dtype=float,
            ),
        ),
        constraints=LinearConstraint(
            matrix,
            np.asarray(lower, dtype=float),
            np.asarray(upper, dtype=float),
        ),
        options=options,
    )
    solver_seconds = monotonic() - solve_started

    status_map = {0: "Optimal", 1: "Stopped", 2: "Infeasible", 3: "Unbounded", 4: "Error"}
    status_name = status_map.get(int(result.status), str(result.status))
    chosen_counts = [0] * len(candidates)
    chosen_indices: list[int] = []
    if result.x is not None:
        for index, raw_value in enumerate(result.x):
            value = int(round(float(raw_value)))
            value = max(0, min(value, int(candidates[index]["upper_bound"])))
            chosen_counts[index] = value
            chosen_indices.extend([index] * value)

    feasible = _verify_recipe_local_solution(
        candidates, chosen_indices, row_needs, exact_count
    )
    proved_optimal = int(result.status) == 0
    proved_infeasible = int(result.status) == 2
    termination = _solver_termination_metadata(
        proved_optimal=proved_optimal,
        proved_infeasible=proved_infeasible,
        feasible=feasible,
        status=status_name,
        solution_status=str(result.message),
        time_limit=time_limit,
        solver_seconds=solver_seconds,
        explicit_time_limit_reached=(int(result.status) == 1 and time_limit is not None),
    )
    return {
        "chosen_indices": chosen_indices,
        "chosen_counts": chosen_counts,
        "status": status_name,
        "solution_status": str(result.message),
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        **termination,
        "objective": float(result.fun) if feasible and result.fun is not None else None,
        "solver_seconds": float(solver_seconds),
        "mip_gap": getattr(result, "mip_gap", None),
        "best_bound": getattr(result, "mip_dual_bound", None),
        "mip_node_count": getattr(result, "mip_node_count", None),
        "warm_start_used": False,
        "warm_start_retry_without_start": False,
    }


def _select_min_density_rectangles_recipes(
    value_matrix: Sequence[Sequence[int]],
    xs: Sequence[float],
    ys: Sequence[float],
    rectangles: Iterable[tuple[int, int, int, int, int]],
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]],
    n: int | None = None,
    *,
    holds: Mapping[int, float] | None = None,
    axis: str | int | None = None,
    mosaic: Sequence[Sequence[int]] | None = None,
    initial_indices: Sequence[int] | None = None,
    cover_zero_cells: bool = False,
    solver=None,
    solver_msg: bool = False,
    time_limit: float | None = None,
    threads: int | None = None,
    require_optimal: bool = True,
    decompose: bool = True,
    greedy_warm_start_limit: int = 2_000,
    backend: str = "pulp",
) -> dict[str, Any]:
    del decompose  # exact N creates one global row; recipe path intentionally uses one MILP.

    if n is not None:
        if isinstance(n, bool) or int(n) != n or int(n) < 0:
            raise ValueError("n должен быть неотрицательным целым или None")
        n = int(n)

    initial_index_list: list[int] | None = None
    if initial_indices is not None:
        if n is None:
            raise ValueError("initial_indices применим только при заданном n")
        initial_index_list = []
        for index in initial_indices:
            if isinstance(index, bool) or int(index) != index:
                raise ValueError("initial_indices должен содержать целые индексы")
            initial_index_list.append(int(index))
        if len(initial_index_list) != n:
            raise ValueError("initial_indices должен содержать ровно n индексов")

    if time_limit is not None:
        time_limit = float(time_limit)
        if not np.isfinite(time_limit) or time_limit <= 0:
            raise ValueError("time_limit должен быть положительным или None")
    if threads is not None:
        if isinstance(threads, bool) or int(threads) != threads or int(threads) <= 0:
            raise ValueError("threads должен быть положительным целым или None")
        threads = int(threads)
    if greedy_warm_start_limit < 0:
        raise ValueError("greedy_warm_start_limit не может быть отрицательным")

    backend = str(backend).lower()
    if backend not in {"pulp", "scipy"}:
        raise ValueError("backend должен быть равен 'pulp' или 'scipy'")
    if backend == "scipy" and solver is not None:
        raise ValueError("Параметр solver применим только к backend='pulp'")

    requirements = np.asarray(value_matrix)
    if requirements.ndim != 2:
        raise ValueError("value_matrix должна быть двумерной")
    if not np.all(np.isfinite(requirements)):
        raise ValueError("value_matrix содержит NaN или бесконечность")
    if not np.all(requirements == np.floor(requirements)):
        raise ValueError("value_matrix должна содержать целые значения")
    requirements = requirements.astype(np.int64, copy=False)
    if np.any(requirements < 0):
        raise ValueError("Значения value_matrix не могут быть отрицательными")

    ny, nx = requirements.shape
    normalized_mosaic = _normalize_mosaic(mosaic, (ny, nx))
    if len(xs) != nx or len(ys) != ny:
        raise ValueError("len(xs)/len(ys) не совпадают с размером матрицы")
    xs_array = np.asarray(xs, dtype=float)
    ys_array = np.asarray(ys, dtype=float)
    if (
        not np.all(np.isfinite(xs_array))
        or not np.all(np.isfinite(ys_array))
        or np.any(xs_array <= 0)
        or np.any(ys_array <= 0)
    ):
        raise ValueError("Все размеры xs и ys должны быть конечными и положительными")

    density: dict[int, float] = {}
    for raw_key, raw_value in densities.items():
        if isinstance(raw_key, bool) or int(raw_key) != raw_key:
            raise ValueError("Ключи densities должны быть целыми")
        key = int(raw_key)
        value = float(raw_value)
        if (
            key < 0
            or not np.isfinite(value)
            or (key == 0 and value < 0)
            or (key > 0 and value <= 0)
        ):
            raise ValueError("Некорректный ключ или значение densities")
        density[key] = value
    if not density:
        raise ValueError("densities не должен быть пустым")
    density.setdefault(0, 0.0)
    density_keys = sorted(density)
    # В recipe-модели densities описывает только реально выбираемые (primitive)
    # классы прямоугольников. Составные классы могут занимать пропуски и
    # задаваться только через recipes, поэтому ключи densities не обязаны
    # образовывать 0, 1, ..., N. Требуем лишь монотонный рост плотности по
    # реально присутствующим primitive-классам.
    if any(
        density[left] >= density[right]
        for left, right in zip(density_keys, density_keys[1:])
    ):
        raise ValueError("Плотности должны строго возрастать с увеличением класса")

    normalized_holds, hold_axis = _normalize_holds_axis(density, holds, axis)
    normalized_recipes = _normalize_recipes(recipes, density)
    classes_present = set(map(int, np.unique(requirements)))
    for class_id in classes_present:
        _class_layers(class_id, density, normalized_recipes)

    x_edges = np.concatenate(([0.0], np.cumsum(xs_array)))
    y_edges = np.concatenate(([0.0], np.cumsum(ys_array)))
    raw_rectangles = list(rectangles)
    if initial_index_list is not None and any(
        index < 0 or index >= len(raw_rectangles) for index in initial_index_list
    ):
        raise ValueError("initial_indices содержит индекс вне rectangles")

    parsed_rectangles: list[dict[str, Any]] = []
    seen_rectangles: set[tuple[int, int, int, int, int]] = set()
    ignored_recipe_rectangles = 0
    for original_index, raw in enumerate(raw_rectangles):
        if len(raw) != 5:
            raise ValueError(
                f"rectangles[{original_index}] должен иметь формат "
                "(xmin, ymin, xmax, ymax, w)"
            )
        if any(isinstance(value, bool) or int(value) != value for value in raw):
            raise ValueError(f"rectangles[{original_index}] должен содержать целые значения")
        xmin, ymin, xmax, ymax, level = map(int, raw)
        rect = (xmin, ymin, xmax, ymax, level)
        if not (0 <= xmin <= xmax < nx and 0 <= ymin <= ymax < ny):
            raise ValueError(
                f"Прямоугольник #{original_index} {rect} выходит за границы матрицы"
            )
        # Recipe keys describe composite REQUIREMENTS, not selectable rectangles.
        # Example: recipes[3] = (1, 2) means class 3 must be formed by
        # overlapping primitive rectangles w=1 and w=2. A raw w=3 candidate
        # is therefore ignored completely and does not count toward exact n.
        if level in normalized_recipes:
            ignored_recipe_rectangles += 1
            continue
        if level not in density:
            raise ValueError(
                f"Для класса прямоугольника w={level} нет densities; "
                "recipe-классы нельзя выбирать как готовые прямоугольники"
            )
        layers = (level,)
        effective_density = float(density[level])
        if n is None and rect in seen_rectangles:
            continue
        seen_rectangles.add(rect)
        base_area, area, hold = _rectangle_area_with_hold(
            x_edges,
            y_edges,
            xmin,
            ymin,
            xmax,
            ymax,
            level,
            normalized_holds,
            hold_axis,
        )
        parsed_rectangles.append(
            {
                "uid": original_index,
                "original_index": original_index,
                "rect": rect,
                "level": level,
                "layers": layers,
                "density": effective_density,
                "base_area": base_area,
                "area": area,
                "hold": hold,
                "hold_axis": hold_axis,
                "cost": float(area * effective_density),
            }
        )

    base_stats: dict[str, Any] = {
        "input_rectangles": len(raw_rectangles),
        "validated_candidates": len(parsed_rectangles),
        "unique_input_rectangles": len(seen_rectangles),
        "n_requested": n,
        "backend": backend,
        "recipes_used": True,
        "recipe_classes": len(normalized_recipes),
        "ignored_recipe_rectangles": ignored_recipe_rectangles,
        "holds_used": bool(normalized_holds),
        "hold_axis": hold_axis,
        "mosaic_used": normalized_mosaic is not None,
    }
    active_mask = np.ones_like(requirements, dtype=bool) if cover_zero_cells else requirements > 0
    active_coordinates = np.argwhere(active_mask)
    base_stats["active_cells"] = int(len(active_coordinates))

    if len(active_coordinates) == 0:
        if n is None:
            selected: list[dict[str, Any]] = []
            selected_local_indices: list[int] = []
        elif n > len(parsed_rectangles):
            return _empty_result(
                status="Infeasible",
                n_requested=n,
                reason=(
                    f"Требуется выбрать n={n}, но доступно только "
                    f"{len(parsed_rectangles)} различных кандидатов"
                ),
                stats=base_stats,
                infeasibility_proved=True,
            )
        else:
            order = sorted(
                range(len(parsed_rectangles)),
                key=lambda index: (
                    parsed_rectangles[index]["cost"],
                    parsed_rectangles[index]["original_index"],
                ),
            )[:n]
            selected_local_indices = order
            selected = [parsed_rectangles[index] for index in order]
            selected.sort(key=lambda candidate: candidate["original_index"])
        total_cost = float(sum(candidate["cost"] for candidate in selected))
        return {
            "rectangles": [candidate["rect"] for candidate in selected],
            "indices": [candidate["original_index"] for candidate in selected],
            "details": [_candidate_details(candidate) for candidate in selected],
            "multiplicities": _multiplicity_details(
                parsed_rectangles, selected_local_indices
            ),
            "total_cost": total_cost,
            "total_weighted_area": total_cost,
            "total_area": float(sum(candidate["area"] for candidate in selected)),
            "total_base_area": float(
                sum(candidate["base_area"] for candidate in selected)
            ),
            "n_rectangles": len(selected),
            "n_requested": n,
            "status": "Optimal",
            "is_feasible": True,
            "is_optimal": True,
            "solver_proved_optimal": True,
            "infeasibility_proved": False,
            "reason": None,
            "stats": {
                **base_stats,
                "usable_candidates": len(parsed_rectangles),
                "recipe_constraints": 0,
                "max_recipe_layers": 0,
                "multiplicity_enabled": True,
                "distinct_rectangles_selected": len(set(selected_local_indices)),
                "repeated_rectangles": 0,
                "strict_count_model": n is not None,
            },
            "component_statuses": [],
        }

    if n == 0:
        return _empty_result(
            status="Infeasible",
            n_requested=0,
            reason="Ненулевая матрица не может быть покрыта нулём прямоугольников",
            stats=base_stats,
            infeasibility_proved=True,
        )

    row_grids: dict[int, np.ndarray] = {}
    row_needs_list: list[int] = []
    row_metadata: list[tuple[int, int, int, int]] = []
    max_recipe_layers = 0
    mosaic_layout: dict[str, Any] | None = None
    unit_rows: list[list[tuple[int, int]]] = []

    if normalized_mosaic is None:
        for y_raw, x_raw in active_coordinates:
            y, x = int(y_raw), int(x_raw)
            class_id = int(requirements[y, x])
            layers = _class_layers(class_id, density, normalized_recipes)
            max_recipe_layers = max(max_recipe_layers, len(layers))
            for threshold, need in _recipe_thresholds(layers):
                grid = row_grids.get(threshold)
                if grid is None:
                    grid = np.full((ny, nx), -1, dtype=np.int32)
                    row_grids[threshold] = grid
                row = len(row_needs_list)
                grid[y, x] = row
                row_needs_list.append(int(need))
                row_metadata.append((y, x, threshold, int(need)))
        base_stats.update(
            {
                "constraints_before_mosaic": len(row_needs_list),
                "constraints_after_mosaic": len(row_needs_list),
                "mosaic_input_elements": len(active_coordinates),
                "mosaic_elements": len(active_coordinates),
            }
        )
    else:
        profile_grid = np.full((ny, nx), -1, dtype=np.int32)
        profile_to_id: dict[tuple[tuple[int, int], ...], int] = {}
        profile_thresholds: list[tuple[tuple[int, int], ...]] = []
        constraints_before_mosaic = 0
        for y_raw, x_raw in active_coordinates:
            y, x = int(y_raw), int(x_raw)
            layers = _class_layers(
                int(requirements[y, x]), density, normalized_recipes
            )
            thresholds = _recipe_thresholds(layers)
            profile = profile_to_id.get(thresholds)
            if profile is None:
                profile = len(profile_thresholds)
                profile_to_id[thresholds] = profile
                profile_thresholds.append(thresholds)
            profile_grid[y, x] = profile
            constraints_before_mosaic += len(thresholds)
            max_recipe_layers = max(max_recipe_layers, len(layers))

        mosaic_layout = _build_mosaic_units(
            active_mask=active_mask,
            mosaic=normalized_mosaic,
            profile_grid=profile_grid,
            candidates=parsed_rectangles,
        )
        unit_rows = [[] for _ in range(int(mosaic_layout["elements"]))]
        for unit, (y, x) in enumerate(mosaic_layout["representatives"]):
            profile = int(mosaic_layout["profiles"][unit])
            for threshold, need in profile_thresholds[profile]:
                row = len(row_needs_list)
                row_needs_list.append(int(need))
                row_metadata.append((int(y), int(x), int(threshold), int(need)))
                unit_rows[unit].append((int(threshold), row))
        base_stats.update(
            {
                "constraints_before_mosaic": constraints_before_mosaic,
                "constraints_after_mosaic": len(row_needs_list),
                "mosaic_input_elements": int(mosaic_layout["input_elements"]),
                "mosaic_elements": int(mosaic_layout["elements"]),
                "mosaic_refinement_splits": (
                    int(mosaic_layout["elements"])
                    - int(mosaic_layout["input_elements"])
                ),
            }
        )

    row_needs = np.asarray(row_needs_list, dtype=np.int32)
    sorted_thresholds = sorted(row_grids)
    coverage_groups: dict[bytes, list[dict[str, Any]]] = defaultdict(list)
    enriched_by_original: dict[int, dict[str, Any]] = {}

    for candidate in parsed_rectangles:
        xmin, ymin, xmax, ymax, _level = candidate["rect"]
        row_parts: list[np.ndarray] = []
        coefficient_parts: list[np.ndarray] = []
        layers = candidate["layers"]
        if mosaic_layout is None:
            for threshold in sorted_thresholds:
                coefficient = sum(layer >= threshold for layer in layers)
                if coefficient == 0:
                    continue
                block = row_grids[threshold][ymin : ymax + 1, xmin : xmax + 1]
                rows_here = block[block >= 0].astype(np.int32, copy=False)
                if rows_here.size:
                    row_parts.append(rows_here)
                    coefficient_parts.append(
                        np.full(rows_here.size, coefficient, dtype=np.int32)
                    )
        else:
            units = _full_units_in_rectangle(
                mosaic_layout["grid"],
                mosaic_layout["sizes"],
                xmin,
                ymin,
                xmax,
                ymax,
            )
            rows_list: list[int] = []
            coefficients_list: list[int] = []
            for unit_raw in units:
                for threshold, row in unit_rows[int(unit_raw)]:
                    coefficient = sum(layer >= threshold for layer in layers)
                    if coefficient:
                        rows_list.append(row)
                        coefficients_list.append(coefficient)
            if rows_list:
                row_parts.append(np.asarray(rows_list, dtype=np.int32))
                coefficient_parts.append(
                    np.asarray(coefficients_list, dtype=np.int32)
                )

        if row_parts:
            rows_array = np.concatenate(row_parts).astype(np.int32, copy=False)
            coefficients = np.concatenate(coefficient_parts).astype(np.int32, copy=False)
            order = np.argsort(rows_array, kind="stable")
            rows_array = rows_array[order]
            coefficients = coefficients[order]
        else:
            rows_array = np.empty(0, dtype=np.int32)
            coefficients = np.empty(0, dtype=np.int32)
        if rows_array.size == 0 and n is None:
            continue
        enriched = dict(candidate)
        enriched["rows"] = rows_array
        enriched["coefficients"] = coefficients
        useful_upper_bound = (
            max(
                int((int(row_needs[row]) + int(coefficient) - 1) // int(coefficient))
                for row, coefficient in zip(rows_array, coefficients)
            )
            if rows_array.size
            else 0
        )
        # Repetition is allowed only while it can contribute to some recipe
        # requirement. For exact n every distinct candidate remains available
        # once as a filler, preserving the legacy exact-count semantics.
        upper_bound = (
            useful_upper_bound
            if n is None
            else min(n, max(1, useful_upper_bound))
        )
        enriched["useful_upper_bound"] = int(useful_upper_bound)
        enriched["upper_bound"] = int(upper_bound)
        signature = rows_array.tobytes() + b"|" + coefficients.tobytes()
        enriched["signature"] = signature
        enriched_by_original[candidate["original_index"]] = enriched
        coverage_groups[signature].append(enriched)

    if initial_index_list is not None:
        initial_candidates = [
            enriched_by_original[index]
            for index in initial_index_list
            if index in enriched_by_original
        ]
        if len(initial_candidates) != n or not _verify_recipe_local_solution(
            initial_candidates,
            list(range(len(initial_candidates))),
            row_needs,
            n,
        ):
            raise ValueError(
                "initial_indices не задаёт допустимое recipe-покрытие для данного n"
            )

    candidates: list[dict[str, Any]] = []
    for group in coverage_groups.values():
        group.sort(
            key=lambda candidate: (candidate["cost"], candidate["original_index"])
        )
        if n is None:
            candidates.append(group[0])
            continue

        capacity = 0
        for candidate in group:
            candidates.append(candidate)
            capacity += int(candidate["upper_bound"])
            if capacity >= n:
                break
    candidates.sort(key=lambda candidate: candidate["original_index"])

    signature_to_locals: dict[bytes, list[int]] = defaultdict(list)
    for index, candidate in enumerate(candidates):
        signature_to_locals[candidate["signature"]].append(index)

    initial_local_indices: list[int] | None = None
    if initial_index_list is not None:
        remaining_capacity = [
            int(candidate["upper_bound"]) for candidate in candidates
        ]
        initial_local_indices = []
        for original_index in initial_index_list:
            signature = enriched_by_original[original_index]["signature"]
            chosen_local = next(
                (
                    local
                    for local in signature_to_locals[signature]
                    if remaining_capacity[local] > 0
                ),
                None,
            )
            if chosen_local is None:
                raise ValueError(
                    "initial_indices превышает допустимую кратность кандидата"
                )
            initial_local_indices.append(chosen_local)
            remaining_capacity[chosen_local] -= 1

    base_stats.update(
        {
            "usable_candidates": len(candidates),
            "recipe_constraints": len(row_needs),
            "max_recipe_layers": max_recipe_layers,
            "multiplicity_enabled": True,
            "max_candidate_multiplicity": max(
                (int(candidate["upper_bound"]) for candidate in candidates),
                default=0,
            ),
            "candidate_row_incidence": int(
                sum(len(candidate["rows"]) for candidate in candidates)
            ),
        }
    )

    if n is not None and n > 0 and not candidates:
        return _empty_result(
            status="Infeasible",
            n_requested=n,
            reason="Нет ни одного выбираемого кандидата для точного n",
            stats=base_stats,
            infeasibility_proved=True,
        )

    if n is not None and sum(
        int(candidate["upper_bound"]) for candidate in candidates
    ) < n:
        return _empty_result(
            status="Infeasible",
            n_requested=n,
            reason="Недостаточная суммарная кратность кандидатов для точного n",
            stats=base_stats,
            infeasibility_proved=True,
        )

    available = np.zeros(len(row_needs), dtype=np.int64)
    for candidate in candidates:
        if candidate["rows"].size:
            available[candidate["rows"]] += (
                candidate["coefficients"].astype(np.int64, copy=False)
                * int(candidate["upper_bound"])
            )
    missing_rows = np.flatnonzero(available < row_needs)
    if missing_rows.size:
        sample = [row_metadata[int(row)] for row in missing_rows[:10]]
        return _empty_result(
            status="Infeasible",
            n_requested=n,
            reason=(
                "Некоторые recipe-требования нельзя выполнить даже с "
                "разрешённой кратностью. Первые (y, x, threshold, need): "
                + str(sample)
            ),
            stats=base_stats,
            infeasibility_proved=True,
        )

    if n == 1:
        feasible_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            supplied = np.zeros(len(row_needs), dtype=np.int32)
            if candidate["rows"].size:
                supplied[candidate["rows"]] += candidate["coefficients"]
            if np.all(supplied >= row_needs):
                feasible_candidates.append(candidate)
        if not feasible_candidates:
            return _empty_result(
                status="Infeasible",
                n_requested=1,
                reason="Нет одного прямоугольника, выполняющего все требования",
                stats={**base_stats, "n1_fast_path": True},
                infeasibility_proved=True,
            )
        chosen = min(
            feasible_candidates,
            key=lambda candidate: (candidate["cost"], candidate["original_index"]),
        )
        return {
            "rectangles": [chosen["rect"]],
            "indices": [chosen["original_index"]],
            "details": [_candidate_details(chosen)],
            "multiplicities": [
                {
                    "index": chosen["original_index"],
                    "rectangle": chosen["rect"],
                    "count": 1,
                }
            ],
            "total_cost": float(chosen["cost"]),
            "total_weighted_area": float(chosen["cost"]),
            "total_area": float(chosen["area"]),
            "total_base_area": float(chosen["base_area"]),
            "n_rectangles": 1,
            "n_requested": 1,
            "status": "Optimal",
            "is_feasible": True,
            "is_optimal": True,
            "solver_proved_optimal": True,
            "infeasibility_proved": False,
            "termination_reason": "optimal",
            "time_limit_reached": False,
            "has_incumbent": True,
            "incumbent_source": "fast_path",
            "reason": None,
            "stats": {
                **base_stats,
                "strict_count_model": True,
                "n1_fast_path": True,
            },
            "component_statuses": [],
        }

    if backend == "pulp":
        subresult = _solve_recipe_with_pulp(
            candidates=candidates,
            row_needs=row_needs,
            exact_count=n,
            solver=solver,
            solver_msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            greedy_warm_start_limit=greedy_warm_start_limit,
            initial_local_indices=initial_local_indices,
        )
    else:
        subresult = _solve_recipe_with_scipy(
            candidates=candidates,
            row_needs=row_needs,
            exact_count=n,
            solver_msg=solver_msg,
            time_limit=time_limit,
        )

    if not subresult["feasible"]:
        if require_optimal and not subresult["proved_infeasible"]:
            raise RuntimeError(
                "Решатель завершился без допустимого recipe-решения и без "
                f"доказательства недопустимости; status={subresult['status']}; "
                f"solution_status={subresult['solution_status']}"
            )
        result = _empty_result(
            status="Infeasible" if subresult["proved_infeasible"] else subresult["status"],
            n_requested=n,
            reason=(
                "Допустимое recipe-решение не найдено; "
                f"solver_status={subresult['status']}; "
                f"solution_status={subresult['solution_status']}"
            ),
            stats={**base_stats, "strict_count_model": n is not None},
            infeasibility_proved=subresult["proved_infeasible"],
        )
        result.update(
            {
                "termination_reason": subresult.get(
                    "termination_reason",
                    "infeasible" if subresult["proved_infeasible"] else "no_solution",
                ),
                "time_limit_reached": bool(subresult.get("time_limit_reached", False)),
            }
        )
        result["stats"].update(
            {
                "solver_seconds": float(subresult.get("solver_seconds", 0.0)),
                "mip_gap": subresult.get("mip_gap"),
                "best_bound": subresult.get("best_bound"),
                "termination_reason": result["termination_reason"],
                "time_limit_reached": result["time_limit_reached"],
            }
        )
        return result

    if require_optimal and not subresult["proved_optimal"]:
        raise RuntimeError(
            "Оптимум recipe-модели не доказан; "
            f"status={subresult['status']}; solution_status={subresult['solution_status']}"
        )

    selected = [candidates[index] for index in subresult["chosen_indices"]]
    if not _verify_recipe_local_solution(
        candidates, subresult["chosen_indices"], row_needs, n
    ):
        raise RuntimeError("Внутренняя ошибка: итоговое recipe-решение недопустимо")
    selected.sort(key=lambda candidate: candidate["original_index"])
    total_cost = float(sum(candidate["cost"] for candidate in selected))
    proved_optimal = bool(subresult["proved_optimal"])

    return {
        "rectangles": [candidate["rect"] for candidate in selected],
        "indices": [candidate["original_index"] for candidate in selected],
        "details": [_candidate_details(candidate) for candidate in selected],
        "multiplicities": _multiplicity_details(
            candidates, subresult["chosen_indices"]
        ),
        "total_cost": total_cost,
        "total_weighted_area": total_cost,
        "total_area": float(sum(candidate["area"] for candidate in selected)),
        "total_base_area": float(
            sum(candidate["base_area"] for candidate in selected)
        ),
        "n_rectangles": len(selected),
        "n_requested": n,
        "status": "Optimal" if proved_optimal else "Feasible",
        "is_feasible": True,
        "is_optimal": proved_optimal,
        "solver_proved_optimal": proved_optimal,
        "infeasibility_proved": False,
        "termination_reason": subresult.get(
            "termination_reason", "optimal" if proved_optimal else "solver_stopped"
        ),
        "time_limit_reached": bool(subresult.get("time_limit_reached", False)),
        "has_incumbent": True,
        "incumbent_source": "solver",
        "reason": None,
        "stats": {
            **base_stats,
            "strict_count_model": n is not None,
            "distinct_rectangles_selected": len(
                set(map(int, subresult["chosen_indices"]))
            ),
            "repeated_rectangles": len(subresult["chosen_indices"])
            - len(set(map(int, subresult["chosen_indices"]))),
            "warm_start_used": subresult.get("warm_start_used", False),
            "warm_start_retry_without_start": subresult.get(
                "warm_start_retry_without_start", False
            ),
            "solver_seconds": float(subresult.get("solver_seconds", 0.0)),
            "mip_gap": subresult.get("mip_gap"),
            "best_bound": subresult.get("best_bound"),
            "mip_node_count": subresult.get("mip_node_count"),
            "termination_reason": subresult.get(
                "termination_reason", "optimal" if proved_optimal else "solver_stopped"
            ),
            "time_limit_reached": bool(subresult.get("time_limit_reached", False)),
        },
        "component_statuses": [
            {
                **subresult,
                "constraints": len(row_needs),
                "candidates": len(candidates),
                "exact_count": n,
            }
        ],
    }



# =============================================================================
# Prepared, reusable model. All geometry, mosaic refinement, hold-adjusted
# objective coefficients, row incidence and optional PuLP model construction
# are performed once by ``prepare_rectangle_problem``.
# =============================================================================

PREPARED_SCHEMA_VERSION = 1
PREPARED_KIND = "select_min_density_rectangles.prepared.v1"
_PREPARED_COUNT_CONSTRAINT = "__exact_rectangle_count__"
_PREPARED_FILE_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}


def _hash_prepared_array(digest, value: Any, dtype: str) -> None:
    array = np.ascontiguousarray(np.asarray(value, dtype=np.dtype(dtype)))
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.tobytes(order="C"))


def _prepared_problem_id(
    value_matrix: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    rectangles: np.ndarray,
    densities: Mapping[int, float],
    recipes: Mapping[int, Sequence[int]],
    holds: Mapping[int, float],
    axis: str | None,
    cover_zero_cells: bool,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"rectangle-cover:prepared-v1:recipe-multiplicity-v2")
    _hash_prepared_array(digest, value_matrix, "<i8")
    _hash_prepared_array(digest, xs, "<f8")
    _hash_prepared_array(digest, ys, "<f8")
    _hash_prepared_array(digest, rectangles.reshape(-1, 5), "<i8")
    digest.update(
        repr(tuple((int(k), float(v)) for k, v in sorted(densities.items()))).encode()
    )
    digest.update(
        repr(
            tuple(
                (int(k), tuple(sorted(map(int, values))))
                for k, values in sorted(recipes.items())
            )
        ).encode()
    )
    digest.update(
        repr(tuple((int(k), float(v)) for k, v in sorted(holds.items()))).encode()
    )
    digest.update((axis or "").encode("ascii"))
    digest.update(b"1" if cover_zero_cells else b"0")
    return digest.hexdigest()


def _objective_scale_value(costs: np.ndarray, objective_scale: str | float | None) -> float:
    if objective_scale is None:
        return 1.0
    if isinstance(objective_scale, str):
        if objective_scale.lower() != "auto":
            raise ValueError("objective_scale должен быть 'auto', положительным числом или None")
        positive = costs[np.isfinite(costs) & (costs > 0)]
        return float(np.median(positive)) if positive.size else 1.0
    scale = float(objective_scale)
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("objective_scale должен быть положительным конечным числом")
    return scale


def _vectorized_rectangle_metrics(
    rects: np.ndarray,
    x_edges: np.ndarray,
    y_edges: np.ndarray,
    density: Mapping[int, float],
    holds: Mapping[int, float],
    axis: str | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized base area, hold area, density, hold and objective cost."""

    if len(rects) == 0:
        empty = np.empty(0, dtype=float)
        return empty, empty, empty, empty, empty

    xmin = rects[:, 0]
    ymin = rects[:, 1]
    xmax = rects[:, 2]
    ymax = rects[:, 3]
    levels = rects[:, 4]

    x0 = x_edges[xmin].astype(float, copy=True)
    x1 = x_edges[xmax + 1].astype(float, copy=True)
    y0 = y_edges[ymin].astype(float, copy=True)
    y1 = y_edges[ymax + 1].astype(float, copy=True)
    base_areas = (x1 - x0) * (y1 - y0)
    hold_values = np.fromiter(
        (float(holds.get(int(level), 0.0)) for level in levels),
        dtype=float,
        count=len(levels),
    )

    if axis == "x":
        x0 = np.maximum(0.0, x0 - hold_values)
        x1 = np.minimum(float(x_edges[-1]), x1 + hold_values)
    elif axis == "y":
        y0 = np.maximum(0.0, y0 - hold_values)
        y1 = np.minimum(float(y_edges[-1]), y1 + hold_values)

    areas = (x1 - x0) * (y1 - y0)
    density_values = np.fromiter(
        (float(density[int(level)]) for level in levels),
        dtype=float,
        count=len(levels),
    )
    return base_areas, areas, density_values, hold_values, areas * density_values


def _candidate_rows_from_representatives(
    rects: np.ndarray,
    row_y: np.ndarray,
    row_x: np.ndarray,
    row_thresholds: np.ndarray,
    *,
    batch_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Build candidate->row incidence by representative points in batches.

    Mosaic refinement guarantees that every returned unit lies either entirely
    inside or entirely outside every selectable rectangle, so a representative
    point is exact and avoids ``np.unique`` over every rectangle submatrix.
    """

    count = len(rects)
    indptr = np.zeros(count + 1, dtype=np.int64)
    chunks: list[np.ndarray] = []
    cursor = 0
    if len(row_y) == 0 or count == 0:
        return indptr, np.empty(0, dtype=np.int32)

    batch_size = max(1, int(batch_size))
    for start in range(0, count, batch_size):
        stop = min(count, start + batch_size)
        block = rects[start:stop]
        inside = (
            (row_x[None, :] >= block[:, 0, None])
            & (row_x[None, :] <= block[:, 2, None])
            & (row_y[None, :] >= block[:, 1, None])
            & (row_y[None, :] <= block[:, 3, None])
            & (row_thresholds[None, :] <= block[:, 4, None])
        )
        local_candidates, rows = np.nonzero(inside)
        counts = np.bincount(local_candidates, minlength=stop - start)
        indptr[start + 1 : stop + 1] = cursor + np.cumsum(counts, dtype=np.int64)
        cursor += int(len(rows))
        if len(rows):
            chunks.append(rows.astype(np.int32, copy=False))

    return indptr, (
        np.concatenate(chunks).astype(np.int32, copy=False)
        if chunks
        else np.empty(0, dtype=np.int32)
    )


def _transpose_candidate_rows(
    candidate_indptr: np.ndarray,
    candidate_rows: np.ndarray,
    row_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    candidate_ids = np.repeat(
        np.arange(len(candidate_indptr) - 1, dtype=np.int32),
        np.diff(candidate_indptr).astype(np.int64, copy=False),
    )
    if candidate_rows.size == 0:
        return np.zeros(row_count + 1, dtype=np.int64), np.empty(0, dtype=np.int32)
    order = np.argsort(candidate_rows, kind="stable")
    sorted_rows = candidate_rows[order]
    counts = np.bincount(sorted_rows, minlength=row_count)
    row_indptr = np.r_[0, np.cumsum(counts, dtype=np.int64)]
    return row_indptr, candidate_ids[order].astype(np.int32, copy=False)


def _subset_candidate_rows(
    all_indptr: np.ndarray,
    all_rows: np.ndarray,
    selected: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    indptr = np.zeros(len(selected) + 1, dtype=np.int64)
    parts: list[np.ndarray] = []
    cursor = 0
    for local, source_raw in enumerate(selected):
        source = int(source_raw)
        part = all_rows[all_indptr[source] : all_indptr[source + 1]]
        if part.size:
            parts.append(part)
            cursor += int(part.size)
        indptr[local + 1] = cursor
    rows = (
        np.concatenate(parts).astype(np.int32, copy=False)
        if parts
        else np.empty(0, dtype=np.int32)
    )
    return indptr, rows


def _make_prepared_pulp_model(prepared: Mapping[str, Any]):
    pulp = _import_pulp()
    model = pulp.LpProblem("minimum_density_rectangle_cover_prepared", pulp.LpMinimize)
    upper_bounds = np.asarray(prepared["variable_upper_bounds"], dtype=np.int32)
    mode = str(prepared["mode"])
    variables = []
    for index, upper_raw in enumerate(upper_bounds):
        upper = int(upper_raw)
        if mode == "legacy":
            variable = pulp.LpVariable(f"prepared_x_{index}", cat=pulp.LpBinary)
        else:
            variable = pulp.LpVariable(
                f"prepared_x_{index}", lowBound=0, upBound=upper, cat=pulp.LpInteger
            )
        variables.append(variable)

    solver_costs = np.asarray(prepared["solver_costs"], dtype=float)
    model += pulp.lpSum(
        float(solver_costs[index]) * variables[index]
        for index in range(len(variables))
    )

    row_needs = np.asarray(prepared["row_needs"], dtype=np.int32)
    raw_row_indptr = prepared.get("row_candidate_indptr")
    raw_row_candidates = prepared.get("row_candidates")
    if raw_row_indptr is None or len(raw_row_indptr) != len(row_needs) + 1:
        row_indptr, row_candidates = _transpose_candidate_rows(
            np.asarray(prepared["candidate_row_indptr"], dtype=np.int64),
            np.asarray(prepared["candidate_rows"], dtype=np.int32),
            len(row_needs),
        )
    else:
        row_indptr = np.asarray(raw_row_indptr, dtype=np.int64)
        row_candidates = np.asarray(raw_row_candidates, dtype=np.int32)
    for row, need_raw in enumerate(row_needs):
        local = row_candidates[row_indptr[row] : row_indptr[row + 1]]
        model += (
            pulp.lpSum(variables[int(index)] for index in local) >= int(need_raw),
            f"prepared_cover_{row}",
        )

    model += (pulp.lpSum(variables) == 0, _PREPARED_COUNT_CONSTRAINT)
    return model, variables


def _build_prepared_pulp_template(prepared: Mapping[str, Any]) -> bytes:
    model, _variables = _make_prepared_pulp_model(prepared)
    return pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)


def prepare_rectangle_problem(
    value_matrix: Sequence[Sequence[int]],
    xs: Sequence[float],
    ys: Sequence[float],
    rectangles: Iterable[tuple[int, int, int, int, int]],
    densities: Mapping[int, float],
    *,
    recipes: Mapping[int, Sequence[int]] | None = None,
    holds: Mapping[int, float] | None = None,
    axis: str | int | None = None,
    mosaic: Sequence[Sequence[int]] | None = None,
    cover_zero_cells: bool = False,
    max_n: int | None = None,
    objective_scale: str | float | None = "auto",
    incidence_batch_size: int = 512,
    build_pulp_template: bool = True,
    compact_after_template: bool = True,
) -> dict[str, Any]:
    """Prepare every N-independent part of the rectangle-cover problem once.

    ``max_n`` should be the largest exact count that will later be requested.
    It permits exact removal of expensive candidates with duplicate coverage.
    The returned mapping is pickleable and may be saved or passed to workers.
    """

    started = monotonic()
    if max_n is not None:
        if isinstance(max_n, bool) or int(max_n) != max_n or int(max_n) < 0:
            raise ValueError("max_n должен быть неотрицательным целым или None")
        max_n = int(max_n)

    requirements = np.asarray(value_matrix)
    if requirements.ndim != 2:
        raise ValueError("value_matrix должна быть двумерной")
    if not np.all(np.isfinite(requirements)) or not np.all(
        requirements == np.floor(requirements)
    ):
        raise ValueError("value_matrix должна содержать конечные целые значения")
    requirements = requirements.astype(np.int64, copy=False)
    if np.any(requirements < 0):
        raise ValueError("Значения value_matrix не могут быть отрицательными")
    ny, nx = requirements.shape

    xs_array = np.asarray(xs, dtype=float)
    ys_array = np.asarray(ys, dtype=float)
    if len(xs_array) != nx or len(ys_array) != ny:
        raise ValueError("len(xs)/len(ys) не совпадают с размером матрицы")
    if (
        not np.all(np.isfinite(xs_array))
        or not np.all(np.isfinite(ys_array))
        or np.any(xs_array <= 0)
        or np.any(ys_array <= 0)
    ):
        raise ValueError("Все размеры xs и ys должны быть конечными и положительными")

    density: dict[int, float] = {}
    for raw_key, raw_value in densities.items():
        if isinstance(raw_key, bool) or int(raw_key) != raw_key:
            raise ValueError("Ключи densities должны быть целыми")
        key = int(raw_key)
        value = float(raw_value)
        if (
            key < 0
            or not np.isfinite(value)
            or (key == 0 and value < 0)
            or (key > 0 and value <= 0)
        ):
            raise ValueError("Некорректный ключ или значение densities")
        density[key] = value
    if not density:
        raise ValueError("densities не должен быть пустым")
    density.setdefault(0, 0.0)
    density_keys = sorted(density)

    normalized_recipes = _normalize_recipes(dict(recipes or {}), density)
    mode = "recipes" if normalized_recipes else "legacy"
    if mode == "legacy":
        nonzero = [key for key in density_keys if key > 0]
        if nonzero and nonzero != list(range(1, nonzero[-1] + 1)):
            raise ValueError(
                "Ключи densities должны идти подряд среди ненулевых классов: 1, ..., N"
            )
    if any(
        density[left] >= density[right]
        for left, right in zip(density_keys, density_keys[1:])
    ):
        raise ValueError("Плотности должны строго возрастать с увеличением класса")

    normalized_holds, hold_axis = _normalize_holds_axis(density, holds, axis)
    normalized_mosaic = _normalize_mosaic(mosaic, (ny, nx))

    classes_present = set(map(int, np.unique(requirements)))
    if mode == "recipes":
        for class_id in classes_present:
            _class_layers(class_id, density, normalized_recipes)
    elif classes_present and max(classes_present) > max(density):
        raise ValueError(
            f"В матрице есть уровень {max(classes_present)}, но максимальный "
            f"класс densities равен {max(density)}"
        )

    raw_rectangle_list = list(rectangles)
    for index, raw in enumerate(raw_rectangle_list):
        if len(raw) != 5:
            raise ValueError(
                f"rectangles[{index}] должен иметь формат (xmin, ymin, xmax, ymax, w)"
            )
        if any(isinstance(value, bool) or int(value) != value for value in raw):
            raise ValueError(f"rectangles[{index}] должен содержать целые значения")
    raw_rectangles = np.asarray(raw_rectangle_list, dtype=np.int64)
    if raw_rectangles.size == 0:
        raw_rectangles = raw_rectangles.reshape(0, 5)
    raw_count = len(raw_rectangles)
    valid_mask = np.ones(raw_count, dtype=bool)
    ignored_recipe_rectangles = 0
    for index, (xmin, ymin, xmax, ymax, level) in enumerate(raw_rectangles):
        if not (0 <= xmin <= xmax < nx and 0 <= ymin <= ymax < ny):
            raise ValueError(
                f"Прямоугольник #{index} {tuple(map(int, raw_rectangles[index]))} "
                "выходит за границы матрицы"
            )
        level = int(level)
        if mode == "recipes" and level in normalized_recipes:
            valid_mask[index] = False
            ignored_recipe_rectangles += 1
        elif level not in density:
            raise ValueError(f"Для класса прямоугольника w={level} нет densities")

    valid_original_indices = np.flatnonzero(valid_mask).astype(np.int64, copy=False)
    all_rects = raw_rectangles[valid_mask].astype(np.int64, copy=False)
    raw_to_valid = np.full(raw_count, -1, dtype=np.int32)
    raw_to_valid[valid_original_indices] = np.arange(len(all_rects), dtype=np.int32)

    x_edges = np.r_[0.0, np.cumsum(xs_array)]
    y_edges = np.r_[0.0, np.cumsum(ys_array)]
    all_base_areas, all_areas, all_density_values, all_hold_values, all_costs = (
        _vectorized_rectangle_metrics(
            all_rects, x_edges, y_edges, density, normalized_holds, hold_axis
        )
    )

    active_mask = (
        np.ones_like(requirements, dtype=bool)
        if cover_zero_cells
        else requirements > 0
    )
    active_coordinates = np.argwhere(active_mask)
    constraints_before_mosaic = 0
    mosaic_layout: dict[str, Any] | None = None

    if mode == "recipes":
        profile_grid = np.full((ny, nx), -1, dtype=np.int32)
        profile_to_id: dict[tuple[tuple[int, int], ...], int] = {}
        profile_thresholds: list[tuple[tuple[int, int], ...]] = []
        for y_raw, x_raw in active_coordinates:
            y, x = int(y_raw), int(x_raw)
            thresholds = _recipe_thresholds(
                _class_layers(int(requirements[y, x]), density, normalized_recipes)
            )
            profile = profile_to_id.get(thresholds)
            if profile is None:
                profile = len(profile_thresholds)
                profile_to_id[thresholds] = profile
                profile_thresholds.append(thresholds)
            profile_grid[y, x] = profile
            constraints_before_mosaic += len(thresholds)
    else:
        profile_grid = requirements.astype(np.int64, copy=False)
        profile_thresholds = []
        constraints_before_mosaic = int(len(active_coordinates))

    if normalized_mosaic is None:
        representatives = [tuple(map(int, yx)) for yx in active_coordinates]
        mosaic_input_elements = len(representatives)
        mosaic_elements = len(representatives)
    else:
        candidate_dicts = [
            {"rect": tuple(map(int, rect))} for rect in all_rects
        ]
        mosaic_layout = _build_mosaic_units(
            active_mask=active_mask,
            mosaic=normalized_mosaic,
            profile_grid=profile_grid,
            candidates=candidate_dicts,
        )
        representatives = list(mosaic_layout["representatives"])
        mosaic_input_elements = int(mosaic_layout["input_elements"])
        mosaic_elements = int(mosaic_layout["elements"])

    row_y_list: list[int] = []
    row_x_list: list[int] = []
    row_threshold_list: list[int] = []
    row_need_list: list[int] = []
    row_metadata: list[tuple[int, int, int, int]] = []
    max_recipe_layers = 1
    for y, x in representatives:
        if mode == "recipes":
            layers = _class_layers(int(requirements[y, x]), density, normalized_recipes)
            max_recipe_layers = max(max_recipe_layers, len(layers))
            thresholds = _recipe_thresholds(layers)
        else:
            thresholds = ((int(requirements[y, x]), 1),)
        for threshold, need in thresholds:
            row_y_list.append(int(y))
            row_x_list.append(int(x))
            row_threshold_list.append(int(threshold))
            row_need_list.append(int(need))
            row_metadata.append((int(y), int(x), int(threshold), int(need)))

    row_y = np.asarray(row_y_list, dtype=np.int32)
    row_x = np.asarray(row_x_list, dtype=np.int32)
    row_thresholds = np.asarray(row_threshold_list, dtype=np.int32)
    row_needs = np.asarray(row_need_list, dtype=np.int32)

    incidence_started = monotonic()
    all_candidate_indptr, all_candidate_rows = _candidate_rows_from_representatives(
        all_rects,
        row_y,
        row_x,
        row_thresholds,
        batch_size=incidence_batch_size,
    )
    incidence_seconds = monotonic() - incidence_started

    all_useful_upper_bounds = np.zeros(len(all_rects), dtype=np.int32)
    for index in range(len(all_rects)):
        rows_here = all_candidate_rows[
            all_candidate_indptr[index] : all_candidate_indptr[index + 1]
        ]
        if rows_here.size:
            all_useful_upper_bounds[index] = int(row_needs[rows_here].max())

    group_map: dict[bytes, list[int]] = defaultdict(list)
    all_group_ids = np.empty(len(all_rects), dtype=np.int32)
    signature_to_group: dict[bytes, int] = {}
    for index in range(len(all_rects)):
        rows_here = all_candidate_rows[
            all_candidate_indptr[index] : all_candidate_indptr[index + 1]
        ]
        signature = rows_here.tobytes()
        group = signature_to_group.get(signature)
        if group is None:
            group = len(signature_to_group)
            signature_to_group[signature] = group
        all_group_ids[index] = group
        group_map[signature].append(index)

    groups_sorted: list[list[int]] = [[] for _ in range(len(signature_to_group))]
    for signature, members in group_map.items():
        group = signature_to_group[signature]
        groups_sorted[group] = sorted(
            members,
            key=lambda index: (
                float(all_costs[index]),
                int(valid_original_indices[index]),
            ),
        )

    selected_all: list[int] = []
    selected_by_group: list[list[int]] = [[] for _ in groups_sorted]
    for group, members in enumerate(groups_sorted):
        if max_n is None:
            kept = members
        elif max_n == 0:
            kept = []
        elif mode == "legacy":
            kept = members[:max_n]
        else:
            kept = []
            capacity = 0
            for index in members:
                kept.append(index)
                capacity += max(1, int(all_useful_upper_bounds[index]))
                if capacity >= max_n:
                    break
        selected_by_group[group] = kept
        selected_all.extend(kept)

    selected_all_array = np.asarray(
        sorted(selected_all, key=lambda i: int(valid_original_indices[i])),
        dtype=np.int32,
    )
    all_to_selected = np.full(len(all_rects), -1, dtype=np.int32)
    all_to_selected[selected_all_array] = np.arange(len(selected_all_array), dtype=np.int32)

    candidate_rectangles = all_rects[selected_all_array]
    candidate_original_indices = valid_original_indices[selected_all_array]
    candidate_base_areas = all_base_areas[selected_all_array]
    candidate_areas = all_areas[selected_all_array]
    candidate_density_values = all_density_values[selected_all_array]
    candidate_hold_values = all_hold_values[selected_all_array]
    candidate_costs = all_costs[selected_all_array]
    candidate_useful_ub = all_useful_upper_bounds[selected_all_array]
    if mode == "legacy":
        variable_upper_bounds = np.ones(len(selected_all_array), dtype=np.int32)
    else:
        variable_upper_bounds = np.maximum(1, candidate_useful_ub).astype(
            np.int32, copy=False
        )

    candidate_indptr, candidate_rows = _subset_candidate_rows(
        all_candidate_indptr, all_candidate_rows, selected_all_array
    )

    row_candidate_indptr, row_candidates = _transpose_candidate_rows(
        candidate_indptr, candidate_rows, len(row_needs)
    )

    group_indptr = np.zeros(len(groups_sorted) + 1, dtype=np.int64)
    group_candidate_parts: list[np.ndarray] = []
    cursor = 0
    for group, kept in enumerate(selected_by_group):
        locals_here = [
            int(all_to_selected[index])
            for index in kept
            if int(all_to_selected[index]) >= 0
        ]
        part = np.asarray(locals_here, dtype=np.int32)
        if part.size:
            group_candidate_parts.append(part)
            cursor += int(part.size)
        group_indptr[group + 1] = cursor
    group_candidates = (
        np.concatenate(group_candidate_parts).astype(np.int32, copy=False)
        if group_candidate_parts
        else np.empty(0, dtype=np.int32)
    )

    raw_to_group = np.full(raw_count, -1, dtype=np.int32)
    raw_to_group[valid_original_indices] = all_group_ids

    # Precompute once: every later warm-start extension uses this stable order.
    # Sorting 25k+ candidates for every N was a major avoidable overhead.
    filler_order = np.lexsort(
        (
            candidate_original_indices.astype(np.int64, copy=False),
            candidate_costs,
        )
    ).astype(np.int32, copy=False)

    scale = _objective_scale_value(candidate_costs, objective_scale)
    solver_costs = candidate_costs / scale
    row_capacity = np.zeros(len(row_needs), dtype=np.int64)
    for candidate in range(len(candidate_rectangles)):
        rows_here = candidate_rows[
            candidate_indptr[candidate] : candidate_indptr[candidate + 1]
        ]
        if rows_here.size:
            row_capacity[rows_here] += int(variable_upper_bounds[candidate])

    problem_id = _prepared_problem_id(
        requirements,
        xs_array,
        ys_array,
        raw_rectangles,
        density,
        normalized_recipes,
        normalized_holds,
        hold_axis,
        cover_zero_cells,
    )

    prepared: dict[str, Any] = {
        "kind": PREPARED_KIND,
        "schema_version": PREPARED_SCHEMA_VERSION,
        "core_version": __version__,
        "problem_id": problem_id,
        "mode": mode,
        "max_n": max_n,
        "requirements_shape": (ny, nx),
        "raw_rectangle_count": raw_count,
        "ignored_recipe_rectangles": ignored_recipe_rectangles,
        "valid_original_indices": valid_original_indices,
        "raw_to_valid": raw_to_valid,
        "raw_to_group": raw_to_group,
        "all_valid_rectangles": all_rects,
        "all_valid_base_areas": all_base_areas,
        "all_valid_areas": all_areas,
        "all_valid_density_values": all_density_values,
        "all_valid_hold_values": all_hold_values,
        "all_valid_costs": all_costs,
        "all_valid_useful_upper_bounds": all_useful_upper_bounds,
        "candidate_rectangles": candidate_rectangles,
        "candidate_original_indices": candidate_original_indices,
        "candidate_base_areas": candidate_base_areas,
        "candidate_areas": candidate_areas,
        "candidate_density_values": candidate_density_values,
        "candidate_hold_values": candidate_hold_values,
        "candidate_costs": candidate_costs,
        "filler_order": filler_order,
        "solver_costs": solver_costs,
        "objective_scale": float(scale),
        "candidate_useful_upper_bounds": candidate_useful_ub,
        "variable_upper_bounds": variable_upper_bounds,
        "candidate_row_indptr": candidate_indptr,
        "candidate_rows": candidate_rows,
        "row_candidate_indptr": row_candidate_indptr,
        "row_candidates": row_candidates,
        "row_needs": row_needs,
        "row_capacity": row_capacity,
        "row_metadata": np.asarray(row_metadata, dtype=np.int64).reshape(-1, 4),
        "group_indptr": group_indptr,
        "group_candidates": group_candidates,
        "holds": dict(normalized_holds),
        "hold_axis": hold_axis,
        "densities": dict(density),
        "recipes": dict(normalized_recipes),
        "cover_zero_cells": bool(cover_zero_cells),
        "stats": {
            "input_rectangles": raw_count,
            "validated_candidates_before_dominance": len(all_rects),
            "usable_candidates": len(candidate_rectangles),
            "candidate_row_incidence": int(len(candidate_rows)),
            "active_cells": int(active_mask.sum()),
            "constraints_before_mosaic": int(constraints_before_mosaic),
            "constraints_after_mosaic": int(len(row_needs)),
            "mosaic_used": normalized_mosaic is not None,
            "mosaic_input_elements": int(mosaic_input_elements),
            "mosaic_elements": int(mosaic_elements),
            "mosaic_refinement_splits": int(mosaic_elements - mosaic_input_elements),
            "recipes_used": mode == "recipes",
            "recipe_classes": len(normalized_recipes),
            "max_recipe_layers": int(max_recipe_layers),
            "multiplicity_enabled": mode == "recipes",
            "holds_used": bool(normalized_holds),
            "hold_axis": hold_axis,
            "max_n": max_n,
            "objective_scale": float(scale),
            "incidence_seconds": float(incidence_seconds),
        },
        "pulp_template_bytes": None,
        "pulp_template_error": None,
    }

    template_started = monotonic()
    if build_pulp_template:
        try:
            prepared["pulp_template_bytes"] = _build_prepared_pulp_template(prepared)
        except Exception as exc:  # template is an optimization, not a correctness dependency
            prepared["pulp_template_error"] = f"{type(exc).__name__}: {exc}"
    prepared["stats"]["pulp_template_seconds"] = float(monotonic() - template_started)
    prepared["stats"]["pulp_template_built"] = prepared["pulp_template_bytes"] is not None
    if compact_after_template and prepared["pulp_template_bytes"] is not None:
        prepared.pop("row_candidate_indptr", None)
        prepared.pop("row_candidates", None)
        prepared["stats"]["row_transpose_compacted"] = True
    else:
        prepared["stats"]["row_transpose_compacted"] = False
    prepared["stats"]["preparation_seconds"] = float(monotonic() - started)
    return prepared


def is_prepared_rectangle_problem(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("kind") == PREPARED_KIND


def save_prepared_rectangle_problem(prepared: Mapping[str, Any], path: str | os.PathLike) -> str:
    prepared = _coerce_prepared_rectangle_problem(prepared)
    destination = os.path.abspath(os.fspath(path))
    with open(destination, "wb") as stream:
        pickle.dump(dict(prepared), stream, protocol=pickle.HIGHEST_PROTOCOL)
    for key in list(_PREPARED_FILE_CACHE):
        if key[0] == destination:
            _PREPARED_FILE_CACHE.pop(key, None)
    return destination


def load_prepared_rectangle_problem(path: str | os.PathLike) -> dict[str, Any]:
    return _coerce_prepared_rectangle_problem(path)


def _coerce_prepared_rectangle_problem(
    prepared: Mapping[str, Any] | str | os.PathLike,
) -> dict[str, Any]:
    if isinstance(prepared, (str, bytes, os.PathLike)):
        path = os.path.abspath(os.fspath(prepared))
        stat = os.stat(path)
        key = (path, int(stat.st_mtime_ns), int(stat.st_size))
        cached = _PREPARED_FILE_CACHE.get(key)
        if cached is not None:
            return cached
        with open(path, "rb") as stream:
            prepared = pickle.load(stream)
        if len(_PREPARED_FILE_CACHE) >= 2:
            _PREPARED_FILE_CACHE.clear()
        if isinstance(prepared, Mapping):
            prepared = dict(prepared)
            prepared.pop("highs_count_indptr", None)
            prepared.pop("highs_count_rows", None)
            _PREPARED_FILE_CACHE[key] = prepared
    if not is_prepared_rectangle_problem(prepared):
        raise ValueError("prepared не является результатом prepare_rectangle_problem")
    if int(prepared.get("schema_version", -1)) != PREPARED_SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая версия prepared")
    prepared = dict(prepared)
    prepared.pop("highs_count_indptr", None)
    prepared.pop("highs_count_rows", None)
    return prepared


def _prepared_candidate_rows(prepared: Mapping[str, Any], candidate: int) -> np.ndarray:
    indptr = np.asarray(prepared["candidate_row_indptr"], dtype=np.int64)
    rows = np.asarray(prepared["candidate_rows"], dtype=np.int32)
    return rows[indptr[candidate] : indptr[candidate + 1]]


def _map_prepared_original_indices(
    prepared: Mapping[str, Any],
    indices: Sequence[int],
    exact_count: int | None,
) -> list[int] | None:
    normalized = [int(index) for index in indices]
    if exact_count is not None and len(normalized) != exact_count:
        return None
    if str(prepared["mode"]) == "legacy" and len(set(normalized)) != len(normalized):
        return None
    if str(prepared["mode"]) == "recipes":
        raw_to_valid = np.asarray(prepared["raw_to_valid"], dtype=np.int32)
        raw_upper = np.asarray(
            prepared["all_valid_useful_upper_bounds"], dtype=np.int32
        )
        for original, count in Counter(normalized).items():
            if original < 0 or original >= len(raw_to_valid):
                return None
            valid = int(raw_to_valid[original])
            if valid < 0 or count > max(1, int(raw_upper[valid])):
                return None

    raw_to_group = np.asarray(prepared["raw_to_group"], dtype=np.int32)
    group_indptr = np.asarray(prepared["group_indptr"], dtype=np.int64)
    group_candidates = np.asarray(prepared["group_candidates"], dtype=np.int32)
    upper_bounds = np.asarray(prepared["variable_upper_bounds"], dtype=np.int32)
    remaining = upper_bounds.astype(np.int64, copy=True)
    mapped: list[int] = []
    for original in normalized:
        if original < 0 or original >= len(raw_to_group):
            return None
        group = int(raw_to_group[original])
        if group < 0:
            return None
        locals_here = group_candidates[
            group_indptr[group] : group_indptr[group + 1]
        ]
        chosen = next(
            (int(local) for local in locals_here if remaining[int(local)] > 0),
            None,
        )
        if chosen is None:
            return None
        remaining[chosen] -= 1
        mapped.append(chosen)

    row_needs = np.asarray(prepared["row_needs"], dtype=np.int32)
    supplied = np.zeros(len(row_needs), dtype=np.int64)
    for candidate, count in Counter(mapped).items():
        rows_here = _prepared_candidate_rows(prepared, int(candidate))
        if rows_here.size:
            supplied[rows_here] += int(count)
    return mapped if np.all(supplied >= row_needs) else None


def verify_prepared_original_indices(
    prepared: Mapping[str, Any] | str | os.PathLike,
    indices: Sequence[int],
    exact_count: int,
) -> bool:
    return _map_prepared_original_indices(
        _coerce_prepared_rectangle_problem(prepared), indices, int(exact_count)
    ) is not None


def extend_prepared_original_indices(
    prepared: Mapping[str, Any] | str | os.PathLike,
    indices: Sequence[int],
    target_count: int,
) -> tuple[list[int], float] | None:
    """Extend one feasible incumbent to ``target_count`` in one linear pass.

    ``prepare_rectangle_problem`` stores ``filler_order`` once. Older prepared
    files remain readable; only for them is the order computed lazily here.
    """

    prepared = _coerce_prepared_rectangle_problem(prepared)
    target_count = int(target_count)
    if len(indices) > target_count:
        return None
    mapped = _map_prepared_original_indices(prepared, indices, len(indices))
    if mapped is None:
        return None

    upper_bounds = np.asarray(prepared["variable_upper_bounds"], dtype=np.int32)
    counts = np.bincount(mapped, minlength=len(upper_bounds)).astype(
        np.int32, copy=False
    )
    missing = target_count - int(counts.sum())

    raw_order = prepared.get("filler_order")
    if raw_order is None:  # backward compatibility with prepared schema v1
        costs = np.asarray(prepared["candidate_costs"], dtype=float)
        order = np.lexsort(
            (
                np.asarray(prepared["candidate_original_indices"], dtype=np.int64),
                costs,
            )
        )
    else:
        order = np.asarray(raw_order, dtype=np.int32)

    # One pass is sufficient: each candidate contributes all remaining useful
    # capacity before moving to the next-cheapest candidate.
    for candidate_raw in order:
        if missing <= 0:
            break
        candidate = int(candidate_raw)
        capacity = int(upper_bounds[candidate]) - int(counts[candidate])
        if capacity <= 0:
            continue
        add = min(capacity, missing)
        counts[candidate] += add
        missing -= add
    if missing:
        return None

    originals = np.asarray(prepared["candidate_original_indices"], dtype=np.int64)
    nonzero = np.flatnonzero(counts)
    result = np.repeat(originals[nonzero], counts[nonzero]).astype(
        np.int64, copy=False
    ).tolist()
    costs = np.asarray(prepared["candidate_costs"], dtype=float)
    return [int(index) for index in result], float(np.dot(counts, costs))

def materialize_prepared_original_indices(
    prepared: Mapping[str, Any] | str | os.PathLike,
    indices: Sequence[int],
    *,
    n_requested: int | None = None,
    status: str = "Feasible",
    is_optimal: bool = False,
) -> dict[str, Any]:
    prepared = _coerce_prepared_rectangle_problem(prepared)
    normalized = [int(index) for index in indices]
    raw_to_valid = np.asarray(prepared["raw_to_valid"], dtype=np.int32)
    valid_rects = np.asarray(prepared["all_valid_rectangles"], dtype=np.int64)
    base_areas = np.asarray(prepared["all_valid_base_areas"], dtype=float)
    areas = np.asarray(prepared["all_valid_areas"], dtype=float)
    densities = np.asarray(prepared["all_valid_density_values"], dtype=float)
    holds = np.asarray(prepared["all_valid_hold_values"], dtype=float)
    costs = np.asarray(prepared["all_valid_costs"], dtype=float)
    local = [int(raw_to_valid[index]) for index in normalized]
    if any(index < 0 for index in local):
        raise ValueError("indices содержит невыбираемый прямоугольник")

    details = []
    for original, index in zip(normalized, local):
        rect = tuple(map(int, valid_rects[index]))
        details.append(
            {
                "index": original,
                "rectangle": rect,
                "w": int(rect[4]),
                "density": float(densities[index]),
                "base_area": float(base_areas[index]),
                "area": float(areas[index]),
                "hold": float(holds[index]),
                "hold_axis": prepared.get("hold_axis"),
                "cost": float(costs[index]),
            }
        )
    counts = Counter(normalized)
    multiplicities = [
        {
            "index": original,
            "rectangle": tuple(map(int, valid_rects[int(raw_to_valid[original])])),
            "count": int(count),
        }
        for original, count in sorted(counts.items())
    ]
    return {
        "rectangles": [tuple(map(int, valid_rects[index])) for index in local],
        "indices": normalized,
        "details": details,
        "multiplicities": multiplicities,
        "total_cost": float(sum(costs[index] for index in local)),
        "total_weighted_area": float(sum(costs[index] for index in local)),
        "total_area": float(sum(areas[index] for index in local)),
        "total_base_area": float(sum(base_areas[index] for index in local)),
        "n_rectangles": len(normalized),
        "n_requested": n_requested,
        "status": status,
        "is_feasible": True,
        "is_optimal": bool(is_optimal),
        "solver_proved_optimal": bool(is_optimal),
        "infeasibility_proved": False,
        "reason": None,
        "stats": {
            **dict(prepared.get("stats", {})),
            "prepared_used": True,
            "cache_hit": True,
            "problem_id": prepared["problem_id"],
        },
        "component_statuses": [],
    }


def _verify_prepared_local_solution(
    prepared: Mapping[str, Any],
    chosen_counts: np.ndarray,
    exact_count: int | None,
) -> bool:
    if np.any(chosen_counts < 0):
        return False
    upper = np.asarray(prepared["variable_upper_bounds"], dtype=np.int32)
    if np.any(chosen_counts > upper):
        return False
    if exact_count is not None and int(chosen_counts.sum()) != int(exact_count):
        return False
    supplied = np.zeros(len(prepared["row_needs"]), dtype=np.int64)
    for candidate in np.flatnonzero(chosen_counts):
        rows_here = _prepared_candidate_rows(prepared, int(candidate))
        if rows_here.size:
            supplied[rows_here] += int(chosen_counts[candidate])
    return bool(np.all(supplied >= np.asarray(prepared["row_needs"], dtype=np.int32)))


def _highs_model_status_is(highspy, status: Any, member: str) -> bool:
    enum = getattr(highspy, "HighsModelStatus", None)
    expected = getattr(enum, member, None) if enum is not None else None
    if expected is not None:
        return status == expected
    return member.removeprefix("k").lower() in str(status).lower().replace("_", "")


def _highs_callback_type(highspy, member: str):
    callback_module = getattr(highspy, "cb", None)
    enum = getattr(callback_module, "HighsCallbackType", None)
    if enum is None:
        enum = getattr(highspy, "HighsCallbackType", None)
    return None if enum is None else getattr(enum, member, None)



def _prepared_highs_matrix(
    prepared: Mapping[str, Any],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Base candidate/coverage CSC matrix. Exact N is added as one HiGHS row."""
    indptr = np.asarray(prepared["candidate_row_indptr"], dtype=np.int64)
    rows = np.asarray(prepared["candidate_rows"], dtype=np.int32)
    needs = np.asarray(prepared["row_needs"], dtype=float)
    return (
        indptr,
        rows,
        np.ones(len(rows), dtype=float),
        needs,
        np.full(len(needs), np.inf, dtype=float),
    )


def _make_prepared_highs_lp(prepared: Mapping[str, Any], highspy):
    indptr, rows, values, row_lower, row_upper = _prepared_highs_matrix(prepared)
    upper = np.asarray(prepared["variable_upper_bounds"], dtype=float).copy()
    lp = highspy.HighsLp()
    lp.num_col_ = int(len(upper))
    lp.num_row_ = int(len(row_lower))
    lp.col_cost_ = np.asarray(prepared["solver_costs"], dtype=np.double)
    lp.col_lower_ = np.zeros(len(upper), dtype=np.double)
    lp.col_upper_ = upper.astype(np.double, copy=False)
    lp.row_lower_ = row_lower.astype(np.double, copy=False)
    lp.row_upper_ = row_upper.astype(np.double, copy=False)
    fmt = getattr(getattr(highspy, "MatrixFormat", None), "kColwise", None)
    if fmt is not None:
        lp.a_matrix_.format_ = fmt
    lp.a_matrix_.start_ = indptr
    lp.a_matrix_.index_ = rows
    lp.a_matrix_.value_ = values.astype(np.double, copy=False)
    integer = getattr(getattr(highspy, "HighsVarType", None), "kInteger", 1)
    lp.integrality_ = [integer] * len(upper)
    return lp, upper.astype(np.int32, copy=False)


def _check_highs_status(status: Any, action: str) -> None:
    if "error" in str(status).lower():
        raise RuntimeError(f"HiGHS error during {action}: {status}")



def _set_highs_option(highs, name: str, value: Any) -> None:
    _check_highs_status(highs.setOptionValue(str(name), value), f"option {name}")

def _set_highs_initial_solution(highs, initial_counts: np.ndarray) -> bool:
    nonzero = np.flatnonzero(initial_counts)
    if nonzero.size == 0:
        return False
    # Sparse setSolution is available in modern highspy and avoids allocating
    # row/dual data. Keep a full-solution fallback for older wrappers.
    indices = nonzero.astype(np.int32, copy=False)
    values = initial_counts[nonzero].astype(np.double, copy=False)
    sparse_method = getattr(highs, "setSparseSolution", None)
    try:
        if callable(sparse_method):
            status = sparse_method(int(nonzero.size), indices, values)
        else:
            status = highs.setSolution(int(nonzero.size), indices, values)
    except TypeError:
        highspy = _import_highspy()
        solution = highspy.HighsSolution()
        solution.col_value = initial_counts.astype(float).tolist()
        if hasattr(solution, "value_valid"):
            solution.value_valid = True
        status = highs.setSolution(solution)
    if "error" in str(status).lower():
        raise RuntimeError(f"HiGHS не принял warm start: {status}")
    return True


def _finite_or_none(value: Any, *, scale: float = 1.0) -> float | None:
    try:
        number = float(value) * float(scale)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None



def _solve_prepared_with_highs(
    prepared: Mapping[str, Any],
    *,
    exact_count: int | None,
    solver_msg: bool,
    time_limit: float | None,
    threads: int | None,
    initial_local_indices: Sequence[int] | None,
    highs_options: Mapping[str, Any] | None,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    highspy = _import_highspy()
    built = monotonic()
    lp, upper = _make_prepared_highs_lp(prepared, highspy)
    highs = highspy.Highs()
    _check_highs_status(highs.passModel(lp), "passModel")

    if exact_count is not None:
        indices = np.arange(len(upper), dtype=np.int32)
        values = np.ones(len(upper), dtype=np.double)
        _check_highs_status(
            highs.addRow(float(exact_count), float(exact_count), len(upper), indices, values),
            "exact-count row",
        )

    options = dict(highs_options or {})
    if threads is not None and int(threads) > 1:
        options.setdefault("parallel", "on")
    for name, value in options.items():
        _set_highs_option(highs, name, value)
    _set_highs_option(highs, "output_flag", bool(solver_msg))
    _set_highs_option(highs, "mip_rel_gap", 0.0)
    _set_highs_option(highs, "mip_abs_gap", 0.0)
    if time_limit is not None:
        _set_highs_option(highs, "time_limit", float(time_limit))
    if threads is not None:
        _set_highs_option(highs, "threads", int(threads))

    initial = np.zeros(len(upper), dtype=np.int32)
    if initial_local_indices is not None:
        initial = np.bincount(
            np.asarray(initial_local_indices, dtype=np.int32), minlength=len(upper)
        ).astype(np.int32, copy=False)
    warm_start = _set_highs_initial_solution(highs, initial)

    # Fail loudly if the model accidentally became an LP relaxation.
    loaded_lp = highs.getLp()
    integer = getattr(getattr(highspy, "HighsVarType", None), "kInteger", 1)
    integrality = list(getattr(loaded_lp, "integrality_", []))
    if len(integrality) != len(upper) or any(value != integer for value in integrality):
        raise RuntimeError("HiGHS model lost integer variable types")

    costs = np.asarray(prepared["candidate_costs"], dtype=float)
    objective_scale = float(prepared.get("objective_scale", 1.0))
    callback_errors: list[str] = []
    solve_started = monotonic()
    improving = _highs_callback_type(highspy, "kCallbackMipImprovingSolution")
    logging = _highs_callback_type(highspy, "kCallbackMipLogging")

    if progress_callback is not None:
        if improving is None:
            raise RuntimeError("Update highspy: MIP improving-solution callback is unavailable")

        def callback(callback_type, message, data_out, data_in, user_data):
            try:
                common = {
                    "elapsed": _finite_or_none(getattr(data_out, "running_time", None))
                    or float(monotonic() - solve_started),
                    "gap": _finite_or_none(getattr(data_out, "mip_gap", None)),
                    "nodes": int(getattr(data_out, "mip_node_count", 0) or 0),
                    "best_bound": _finite_or_none(
                        getattr(data_out, "mip_dual_bound", None), scale=objective_scale
                    ),
                    "message": str(message or ""),
                }
                if callback_type == improving:
                    raw = getattr(data_out, "mip_solution", None)
                    if raw is None:
                        return
                    values = np.asarray(list(raw), dtype=float)
                    if values.size != len(upper):
                        return
                    counts = np.rint(values).astype(np.int32)
                    counts = np.maximum(0, np.minimum(counts, upper))
                    if _verify_prepared_local_solution(prepared, counts, exact_count):
                        progress_callback({
                            "kind": "incumbent",
                            "chosen_counts": counts.copy(),
                            "total_cost": float(np.dot(counts, costs)),
                            **common,
                        })
                elif logging is not None and callback_type == logging:
                    progress_callback({"kind": "progress", **common})
            except BaseException as exc:
                callback_errors.append(f"{type(exc).__name__}: {exc}")

        _check_highs_status(highs.setCallback(callback, None), "setCallback")
        _check_highs_status(highs.startCallback(improving), "improving callback")
        if logging is not None and getattr(progress_callback, "include_logging", False):
            _check_highs_status(highs.startCallback(logging), "logging callback")

    model_seconds = monotonic() - built
    run_status = highs.run()
    _check_highs_status(run_status, "run")
    solver_seconds = monotonic() - solve_started
    model_status = highs.getModelStatus()
    status_name = str(highs.modelStatusToString(model_status))
    info = highs.getInfo()
    raw_values = list(getattr(highs.getSolution(), "col_value", []))
    values = np.asarray(raw_values, dtype=float)
    counts = np.zeros(len(upper), dtype=np.int32)
    fractionality = float("inf")
    if values.size == len(upper):
        rounded = np.rint(values)
        fractionality = float(np.max(np.abs(values - rounded), initial=0.0))
        counts = np.maximum(0, np.minimum(rounded.astype(np.int32), upper))

    feasible = _verify_prepared_local_solution(prepared, counts, exact_count)
    actual_objective = float(np.dot(counts, costs)) if feasible else None
    native_objective = _finite_or_none(getattr(info, "objective_function_value", None))
    scale = objective_scale
    native_actual = None if native_objective is None else native_objective * scale
    objective_ok = bool(
        feasible and native_actual is not None
        and abs(actual_objective - native_actual)
        <= 1e-7 * max(1.0, abs(actual_objective), abs(native_actual))
    )
    gap = _finite_or_none(getattr(info, "mip_gap", None))
    native_optimal = _highs_model_status_is(highspy, model_status, "kOptimal")
    proved_optimal = bool(
        native_optimal and feasible and fractionality <= 1e-6 and objective_ok
        and (gap is None or gap <= 1e-9)
    )
    proved_infeasible = bool(
        _highs_model_status_is(highspy, model_status, "kInfeasible") and not feasible
    )
    time_limit_hit = _highs_model_status_is(highspy, model_status, "kTimeLimit")
    termination = _solver_termination_metadata(
        proved_optimal=proved_optimal,
        proved_infeasible=proved_infeasible,
        feasible=feasible,
        status=status_name,
        solution_status=status_name,
        time_limit=time_limit,
        solver_seconds=solver_seconds,
        explicit_time_limit_reached=time_limit_hit,
    )
    validation_error = None
    if native_optimal and not proved_optimal:
        validation_error = (
            f"native optimal rejected: feasible={feasible}, "
            f"fractionality={fractionality:.3g}, objective_match={objective_ok}, gap={gap}"
        )
        termination = {"termination_reason": "optimality_validation_failed", "time_limit_reached": False, "has_incumbent": feasible}

    try:
        version = str(highs.version())
    except Exception:
        version = getattr(highspy, "__version__", None)
    return {
        "chosen_counts": counts,
        "status": status_name,
        "solution_status": status_name,
        "highs_run_status": str(run_status),
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        **termination,
        "warm_start_used": warm_start,
        "warm_start_retry_without_start": False,
        "pulp_template_used": False,
        "model_materialization_seconds": float(model_seconds),
        "solver_seconds": float(solver_seconds),
        "mip_gap": gap,
        "best_bound": _finite_or_none(getattr(info, "mip_dual_bound", None), scale=scale),
        "mip_node_count": int(getattr(info, "mip_node_count", 0) or 0),
        "native_objective": native_objective,
        "native_objective_actual_units": native_actual,
        "actual_objective": actual_objective,
        "objective_consistent": objective_ok,
        "max_fractionality": fractionality,
        "optimality_validation_error": validation_error,
        "solver_class": "highspy.Highs",
        "solver_path": None,
        "threads_requested": threads,
        "highs_version": version,
        "callback_errors": callback_errors,
    }

def _solve_prepared_with_pulp(
    prepared: Mapping[str, Any],
    *,
    exact_count: int | None,
    solver,
    solver_msg: bool,
    time_limit: float | None,
    threads: int | None,
    initial_local_indices: Sequence[int] | None,
    pulp_solver: str,
    highs_options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    pulp = _import_pulp()
    materialize_started = monotonic()
    template = prepared.get("pulp_template_bytes")
    template_load_error: str | None = None
    if template is not None:
        try:
            model = pickle.loads(template)
            variable_map = model.variablesDict()
            variables = [
                variable_map[f"prepared_x_{index}"]
                for index in range(len(prepared["candidate_rectangles"]))
            ]
            template_used = True
        except Exception as exc:
            template_load_error = f"{type(exc).__name__}: {exc}"
            model, variables = _make_prepared_pulp_model(prepared)
            template_used = False
    else:
        model, variables = _make_prepared_pulp_model(prepared)
        template_used = False

    count_constraint = model.constraints.get(_PREPARED_COUNT_CONSTRAINT)
    if exact_count is None:
        if count_constraint is not None:
            del model.constraints[_PREPARED_COUNT_CONSTRAINT]
        useful = np.asarray(prepared["candidate_useful_upper_bounds"], dtype=np.int32)
        for index, variable in enumerate(variables):
            rows_here = _prepared_candidate_rows(prepared, index)
            variable.upBound = int(useful[index]) if rows_here.size else 0
    elif count_constraint is None:
        model += (pulp.lpSum(variables) == int(exact_count), _PREPARED_COUNT_CONSTRAINT)
    else:
        count_constraint.changeRHS(int(exact_count))

    initial_counts = Counter(map(int, initial_local_indices or []))
    warm_start = initial_local_indices is not None
    if warm_start:
        for index, variable in enumerate(variables):
            variable.setInitialValue(int(initial_counts.get(index, 0)))

    model_seconds = monotonic() - materialize_started
    selected_pulp_solver = _normalize_pulp_solver_name(pulp_solver)
    active_solver = solver
    if active_solver is None:
        active_solver = _make_pulp_solver(
            name=selected_pulp_solver,
            msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            warm_start=warm_start,
            highs_options=highs_options,
        )

    solve_started = monotonic()
    warm_start_retry = False
    try:
        model.solve(active_solver)
    except pulp.PulpSolverError:
        if solver is not None or not warm_start:
            raise
        warm_start_retry = True
        elapsed_before_retry = monotonic() - solve_started
        remaining_limit = (
            None
            if time_limit is None
            else max(0.001, float(time_limit) - elapsed_before_retry)
        )
        active_solver = _make_pulp_solver(
            name=selected_pulp_solver,
            msg=solver_msg,
            time_limit=remaining_limit,
            threads=threads,
            warm_start=False,
            highs_options=highs_options,
        )
        model.solve(active_solver)
    solver_seconds = monotonic() - solve_started

    status_name = pulp.LpStatus.get(model.status, str(model.status))
    solution_code = getattr(model, "sol_status", None)
    solution_name = getattr(pulp, "LpSolution", {}).get(solution_code, str(solution_code))
    optimal_solution_code = getattr(pulp, "LpSolutionOptimal", 1)
    integer_feasible_code = getattr(pulp, "LpSolutionIntegerFeasible", 2)
    proved_optimal = (
        model.status == pulp.LpStatusOptimal
        and (solution_code is None or solution_code == optimal_solution_code)
    )
    proved_infeasible = model.status == pulp.LpStatusInfeasible
    upper = np.asarray(prepared["variable_upper_bounds"], dtype=np.int32)
    chosen_counts = np.zeros(len(variables), dtype=np.int32)
    for index, variable in enumerate(variables):
        value = 0 if variable.varValue is None else int(round(float(variable.varValue)))
        chosen_counts[index] = max(0, min(value, int(upper[index])))
    feasible = _verify_prepared_local_solution(prepared, chosen_counts, exact_count)
    termination = _solver_termination_metadata(
        proved_optimal=proved_optimal,
        proved_infeasible=proved_infeasible,
        feasible=feasible,
        status=status_name,
        solution_status=solution_name,
        time_limit=time_limit,
        solver_seconds=solver_seconds,
        explicit_time_limit_reached=(
            True
            if solution_code == integer_feasible_code and time_limit is not None
            else None
        ),
    )

    native_info = None
    native_model = getattr(model, "solverModel", None)
    if native_model is not None and hasattr(native_model, "getInfo"):
        try:
            native_info = native_model.getInfo()
        except Exception:
            native_info = None
    objective_scale = float(prepared.get("objective_scale", 1.0))
    mip_gap = None if native_info is None else _finite_or_none(
        getattr(native_info, "mip_gap", None)
    )
    best_bound = None if native_info is None else _finite_or_none(
        getattr(native_info, "mip_dual_bound", None), scale=objective_scale
    )
    mip_node_count = None if native_info is None else int(
        getattr(native_info, "mip_node_count", 0) or 0
    )
    return {
        "chosen_counts": chosen_counts,
        "status": status_name,
        "solution_status": solution_name,
        "solution_code": solution_code,
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        **termination,
        "warm_start_used": warm_start,
        "warm_start_retry_without_start": warm_start_retry,
        "pulp_template_used": template_used,
        "pulp_template_load_error": template_load_error,
        "model_materialization_seconds": float(model_seconds),
        "solver_seconds": float(solver_seconds),
        "mip_gap": mip_gap,
        "best_bound": best_bound,
        "mip_node_count": mip_node_count,
        "solver_class": type(active_solver).__name__,
        "solver_path": str(getattr(active_solver, "path", "")),
        "pulp_solver": selected_pulp_solver,
        "threads_requested": threads,
    }


def _solve_prepared_with_scipy(
    prepared: Mapping[str, Any],
    *,
    exact_count: int | None,
    solver_msg: bool,
    time_limit: float | None,
) -> dict[str, Any]:
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix, vstack

    build_started = monotonic()
    candidate_indptr = np.asarray(prepared["candidate_row_indptr"], dtype=np.int64)
    rows = np.asarray(prepared["candidate_rows"], dtype=np.int32)
    columns = np.repeat(
        np.arange(len(candidate_indptr) - 1, dtype=np.int32),
        np.diff(candidate_indptr).astype(np.int64, copy=False),
    )
    matrix = coo_matrix(
        (np.ones(len(rows), dtype=float), (rows, columns)),
        shape=(len(prepared["row_needs"]), len(prepared["candidate_rectangles"])),
    ).tocsr()
    lower = np.asarray(prepared["row_needs"], dtype=float)
    upper_rows = np.full(len(lower), np.inf, dtype=float)
    if exact_count is not None:
        matrix = vstack(
            [matrix, np.ones((1, matrix.shape[1]), dtype=float)], format="csr"
        )
        lower = np.r_[lower, float(exact_count)]
        upper_rows = np.r_[upper_rows, float(exact_count)]
    build_seconds = monotonic() - build_started

    upper_bounds = np.asarray(prepared["variable_upper_bounds"], dtype=float).copy()
    if exact_count is None:
        useful = np.asarray(prepared["candidate_useful_upper_bounds"], dtype=float)
        no_rows = np.diff(candidate_indptr) == 0
        upper_bounds = useful
        upper_bounds[no_rows] = 0

    options: dict[str, Any] = {"disp": solver_msg, "mip_rel_gap": 0.0}
    if time_limit is not None:
        options["time_limit"] = float(time_limit)
    solve_started = monotonic()
    result = milp(
        c=np.asarray(prepared["solver_costs"], dtype=float),
        integrality=np.ones(len(upper_bounds), dtype=np.int8),
        bounds=Bounds(np.zeros(len(upper_bounds), dtype=float), upper_bounds),
        constraints=LinearConstraint(matrix, lower, upper_rows),
        options=options,
    )
    solver_seconds = monotonic() - solve_started
    chosen_counts = np.zeros(len(upper_bounds), dtype=np.int32)
    if result.x is not None:
        chosen_counts = np.rint(result.x).astype(np.int32)
        chosen_counts = np.maximum(0, np.minimum(chosen_counts, upper_bounds.astype(np.int32)))
    feasible = _verify_prepared_local_solution(prepared, chosen_counts, exact_count)
    status_map = {0: "Optimal", 1: "Stopped", 2: "Infeasible", 3: "Unbounded", 4: "Error"}
    status_name = status_map.get(int(result.status), str(result.status))
    proved_optimal = int(result.status) == 0
    proved_infeasible = int(result.status) == 2
    termination = _solver_termination_metadata(
        proved_optimal=proved_optimal,
        proved_infeasible=proved_infeasible,
        feasible=feasible,
        status=status_name,
        solution_status=str(result.message),
        time_limit=time_limit,
        solver_seconds=solver_seconds,
        explicit_time_limit_reached=(int(result.status) == 1 and time_limit is not None),
    )
    return {
        "chosen_counts": chosen_counts,
        "status": status_name,
        "solution_status": str(result.message),
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        **termination,
        "warm_start_used": False,
        "warm_start_retry_without_start": False,
        "pulp_template_used": False,
        "model_materialization_seconds": float(build_seconds),
        "solver_seconds": float(solver_seconds),
        "mip_gap": getattr(result, "mip_gap", None),
        "best_bound": getattr(result, "mip_dual_bound", None),
        "mip_node_count": getattr(result, "mip_node_count", None),
        "threads_requested": None,
    }


def _empty_prepared_result(
    prepared: Mapping[str, Any],
    *,
    n: int | None,
    status: str,
    reason: str,
    infeasibility_proved: bool,
) -> dict[str, Any]:
    return _empty_result(
        status=status,
        n_requested=n,
        reason=reason,
        infeasibility_proved=infeasibility_proved,
        stats={
            **dict(prepared.get("stats", {})),
            "prepared_used": True,
            "problem_id": prepared["problem_id"],
            "strict_count_model": n is not None,
        },
    )


def _solve_prepared_rectangle_problem(
    prepared: Mapping[str, Any] | str | os.PathLike,
    n: int | None,
    *,
    initial_indices: Sequence[int] | None,
    solver,
    solver_msg: bool,
    time_limit: float | None,
    threads: int | None,
    require_optimal: bool,
    backend: str,
    pulp_solver: str = "cbc",
    highs_options: Mapping[str, Any] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    total_started = monotonic()
    load_started = monotonic()
    prepared = _coerce_prepared_rectangle_problem(prepared)
    load_seconds = monotonic() - load_started

    if n is not None:
        if isinstance(n, bool) or int(n) != n or int(n) < 0:
            raise ValueError("n должен быть неотрицательным целым или None")
        n = int(n)
    max_n = prepared.get("max_n")
    if n is not None and max_n is not None and n > int(max_n):
        raise ValueError(
            f"prepared построен для max_n={max_n}, но запрошено n={n}; "
            "пересоздайте prepared с большим max_n"
        )
    if time_limit is not None:
        time_limit = float(time_limit)
        if not np.isfinite(time_limit) or time_limit <= 0:
            raise ValueError("time_limit должен быть положительным или None")
    if threads is not None:
        if isinstance(threads, bool) or int(threads) != threads or int(threads) <= 0:
            raise ValueError("threads должен быть положительным целым или None")
        threads = int(threads)
    backend = str(backend).strip().lower()
    if backend not in {"pulp", "highs", "scipy"}:
        raise ValueError("backend должен быть равен 'pulp', 'highs' или 'scipy'")
    selected_pulp_solver = _normalize_pulp_solver_name(pulp_solver)
    if backend != "pulp" and solver is not None:
        raise ValueError("Параметр solver применим только к backend='pulp'")
    if progress_callback is not None and backend != "highs":
        raise ValueError(
            "progress_callback поддерживается только прямым backend='highs'"
        )

    row_needs = np.asarray(prepared["row_needs"], dtype=np.int32)
    upper_bounds = np.asarray(prepared["variable_upper_bounds"], dtype=np.int32)
    candidate_costs = np.asarray(prepared["candidate_costs"], dtype=float)
    candidate_originals = np.asarray(prepared["candidate_original_indices"], dtype=np.int64)

    if len(row_needs) == 0:
        if n is None:
            chosen_counts = np.zeros(len(upper_bounds), dtype=np.int32)
        else:
            if n > int(upper_bounds.sum()):
                return _empty_prepared_result(
                    prepared,
                    n=n,
                    status="Infeasible",
                    reason="Недостаточно кандидатов для точного n",
                    infeasibility_proved=True,
                )
            chosen_counts = np.zeros(len(upper_bounds), dtype=np.int32)
            order = np.lexsort((candidate_originals, candidate_costs))
            remaining = n
            for candidate in order:
                add = min(remaining, int(upper_bounds[candidate]))
                chosen_counts[candidate] = add
                remaining -= add
                if remaining == 0:
                    break
        subresult = {
            "chosen_counts": chosen_counts,
            "status": "Optimal",
            "solution_status": "trivial",
            "proved_optimal": True,
            "proved_infeasible": False,
            "feasible": True,
            "model_materialization_seconds": 0.0,
            "solver_seconds": 0.0,
            "pulp_template_used": False,
            "warm_start_used": False,
            "threads_requested": threads,
        }
    else:
        if n == 0:
            return _empty_prepared_result(
                prepared,
                n=n,
                status="Infeasible",
                reason="Ненулевая матрица не может быть покрыта нулём прямоугольников",
                infeasibility_proved=True,
            )
        if n is not None and n > int(upper_bounds.sum()):
            return _empty_prepared_result(
                prepared,
                n=n,
                status="Infeasible",
                reason="Недостаточная суммарная кратность кандидатов для точного n",
                infeasibility_proved=True,
            )
        missing = np.flatnonzero(
            np.asarray(prepared["row_capacity"], dtype=np.int64) < row_needs
        )
        if missing.size:
            metadata = np.asarray(prepared["row_metadata"], dtype=np.int64)
            sample = metadata[missing[:10]].tolist()
            return _empty_prepared_result(
                prepared,
                n=n,
                status="Infeasible",
                reason=f"Некоторые требования не покрываются. Первые строки: {sample}",
                infeasibility_proved=True,
            )

        initial_local: list[int] | None = None
        if initial_indices is not None:
            initial_local = _map_prepared_original_indices(prepared, initial_indices, n)
            if initial_local is None:
                raise ValueError("initial_indices не задаёт допустимое подготовленное решение")

        if n == 1:
            feasible_candidates = []
            for candidate in range(len(upper_bounds)):
                counts = np.zeros(len(upper_bounds), dtype=np.int32)
                counts[candidate] = 1
                if _verify_prepared_local_solution(prepared, counts, 1):
                    feasible_candidates.append(candidate)
            if not feasible_candidates:
                return _empty_prepared_result(
                    prepared,
                    n=1,
                    status="Infeasible",
                    reason="Нет одного прямоугольника, выполняющего все требования",
                    infeasibility_proved=True,
                )
            chosen = min(
                feasible_candidates,
                key=lambda index: (candidate_costs[index], candidate_originals[index]),
            )
            chosen_counts = np.zeros(len(upper_bounds), dtype=np.int32)
            chosen_counts[chosen] = 1
            subresult = {
                "chosen_counts": chosen_counts,
                "status": "Optimal",
                "solution_status": "n=1 fast path",
                "proved_optimal": True,
                "proved_infeasible": False,
                "feasible": True,
                "model_materialization_seconds": 0.0,
                "solver_seconds": 0.0,
                "pulp_template_used": False,
                "warm_start_used": False,
                "threads_requested": threads,
                "n1_fast_path": True,
            }
        elif backend == "pulp":
            subresult = _solve_prepared_with_pulp(
                prepared,
                exact_count=n,
                solver=solver,
                solver_msg=solver_msg,
                time_limit=time_limit,
                threads=threads,
                initial_local_indices=initial_local,
                pulp_solver=selected_pulp_solver,
                highs_options=highs_options,
            )
        elif backend == "highs":
            subresult = _solve_prepared_with_highs(
                prepared,
                exact_count=n,
                solver_msg=solver_msg,
                time_limit=time_limit,
                threads=threads,
                initial_local_indices=initial_local,
                highs_options=highs_options,
                progress_callback=progress_callback,
            )
        else:
            subresult = _solve_prepared_with_scipy(
                prepared,
                exact_count=n,
                solver_msg=solver_msg,
                time_limit=time_limit,
            )

    if not subresult["feasible"]:
        if require_optimal and not subresult["proved_infeasible"]:
            raise RuntimeError(
                "Решатель завершился без допустимого решения и без доказательства "
                f"недопустимости; status={subresult['status']}; "
                f"solution_status={subresult['solution_status']}"
            )
        result = _empty_prepared_result(
            prepared,
            n=n,
            status="Infeasible" if subresult["proved_infeasible"] else subresult["status"],
            reason=(
                "Допустимое решение не найдено; "
                f"solver_status={subresult['status']}; "
                f"solution_status={subresult['solution_status']}"
            ),
            infeasibility_proved=bool(subresult["proved_infeasible"]),
        )
        result.update(
            {
                "termination_reason": subresult.get(
                    "termination_reason",
                    "infeasible" if subresult["proved_infeasible"] else "no_solution",
                ),
                "time_limit_reached": bool(
                    subresult.get("time_limit_reached", False)
                ),
                "has_incumbent": False,
                "incumbent_source": None,
                "incumbent_is_solver_best": False,
            }
        )
        result["stats"].update(
            {
                "solver_seconds": float(subresult.get("solver_seconds", 0.0)),
                "solver_status": subresult.get("status"),
                "solver_solution_status": subresult.get("solution_status"),
                "mip_gap": subresult.get("mip_gap"),
                "best_bound": subresult.get("best_bound"),
                "termination_reason": result["termination_reason"],
                "time_limit_reached": result["time_limit_reached"],
                "backend": backend,
                "pulp_solver": selected_pulp_solver if backend == "pulp" else None,
                "solver_class": subresult.get("solver_class"),
                "threads_requested": threads,
            }
        )
        return result
    if require_optimal and not subresult["proved_optimal"]:
        raise RuntimeError(
            "Оптимум не доказан; "
            f"status={subresult['status']}; solution_status={subresult['solution_status']}"
        )

    chosen_counts = np.asarray(subresult["chosen_counts"], dtype=np.int32)
    chosen_local = [
        candidate
        for candidate, count in enumerate(chosen_counts)
        for _ in range(int(count))
    ]
    original_indices = [int(candidate_originals[index]) for index in chosen_local]
    result = materialize_prepared_original_indices(
        prepared,
        original_indices,
        n_requested=n,
        status="Optimal" if subresult["proved_optimal"] else "Feasible",
        is_optimal=bool(subresult["proved_optimal"]),
    )
    result.update(
        {
            "termination_reason": subresult.get(
                "termination_reason",
                "optimal" if subresult["proved_optimal"] else "solver_stopped",
            ),
            "time_limit_reached": bool(subresult.get("time_limit_reached", False)),
            "has_incumbent": True,
            "incumbent_source": "solver",
            "incumbent_is_solver_best": True,
        }
    )
    result["stats"].update(
        {
            "cache_hit": False,
            "prepared_load_seconds": float(load_seconds),
            "model_materialization_seconds": float(
                subresult.get("model_materialization_seconds", 0.0)
            ),
            "solver_seconds": float(subresult.get("solver_seconds", 0.0)),
            "solver_status": subresult.get("status"),
            "solver_solution_status": subresult.get("solution_status"),
            "mip_gap": subresult.get("mip_gap"),
            "best_bound": subresult.get("best_bound"),
            "mip_node_count": subresult.get("mip_node_count"),
            "termination_reason": result["termination_reason"],
            "time_limit_reached": result["time_limit_reached"],
            "total_solve_call_seconds": float(monotonic() - total_started),
            "pulp_template_used": bool(subresult.get("pulp_template_used", False)),
            "pulp_template_load_error": subresult.get("pulp_template_load_error"),
            "warm_start_used": bool(subresult.get("warm_start_used", False)),
            "warm_start_retry_without_start": bool(
                subresult.get("warm_start_retry_without_start", False)
            ),
            "threads_requested": threads,
            "solver_class": subresult.get("solver_class"),
            "solver_path": subresult.get("solver_path"),
            "backend": backend,
            "pulp_solver": selected_pulp_solver if backend == "pulp" else None,
            "highs_version": subresult.get("highs_version"),
            "callback_errors": subresult.get("callback_errors", []),
            "objective_consistent": subresult.get("objective_consistent"),
            "max_fractionality": subresult.get("max_fractionality"),
            "optimality_validation_error": subresult.get("optimality_validation_error"),
            "n1_fast_path": bool(subresult.get("n1_fast_path", False)),
        }
    )
    component_status = dict(subresult)
    if isinstance(component_status.get("chosen_counts"), np.ndarray):
        component_status["chosen_counts"] = component_status["chosen_counts"].tolist()
    result["component_statuses"] = [
        {
            **component_status,
            "constraints": len(row_needs),
            "candidates": len(upper_bounds),
            "exact_count": n,
        }
    ]
    return result


def _finalize_public_result_metadata(result: dict[str, Any]) -> dict[str, Any]:
    feasible = bool(result.get("is_feasible"))
    optimal = bool(result.get("is_optimal"))
    infeasible = bool(result.get("infeasibility_proved"))
    result.setdefault(
        "termination_reason",
        "optimal" if optimal else "infeasible" if infeasible else "solver_stopped" if feasible else "no_solution",
    )
    result.setdefault("time_limit_reached", False)
    result.setdefault("has_incumbent", feasible)
    result.setdefault(
        "incumbent_source",
        "solver" if feasible and not result.get("stats", {}).get("cache_hit") else None,
    )
    result.setdefault(
        "incumbent_is_solver_best",
        bool(feasible and result.get("incumbent_source") == "solver"),
    )
    stats = result.setdefault("stats", {})
    stats.setdefault("termination_reason", result["termination_reason"])
    stats.setdefault("time_limit_reached", bool(result["time_limit_reached"]))
    return result


def select_min_density_rectangles(
    value_matrix: Sequence[Sequence[int]] | None = None,
    xs: Sequence[float] | None = None,
    ys: Sequence[float] | None = None,
    rectangles: Iterable[tuple[int, int, int, int, int]] | None = None,
    densities: Mapping[int, float] | None = None,
    n: int | None = None,
    *,
    prepared: Mapping[str, Any] | str | os.PathLike | None = None,
    recipes: Mapping[int, Sequence[int]] | None = None,
    holds: Mapping[int, float] | None = None,
    axis: str | int | None = None,
    mosaic: Sequence[Sequence[int]] | None = None,
    initial_indices: Sequence[int] | None = None,
    cover_zero_cells: bool = False,
    solver=None,
    solver_msg: bool = False,
    time_limit: float | None = None,
    threads: int | None = None,
    require_optimal: bool = True,
    decompose: bool = True,
    greedy_warm_start_limit: int = 2_000,
    backend: str = "pulp",
    pulp_solver: str = "cbc",
    highs_options: Mapping[str, Any] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Solve the original inputs or an N-independent prepared problem.

    Recommended repeated-N workflow::

        prepared = prepare_rectangle_problem(..., max_n=87)
        result = select_min_density_rectangles(
            prepared=prepared, n=30, backend="pulp", threads=1
        )

    With ``prepared`` all geometry, mosaic, hold costs, coverage incidence,
    dominance groups and (when PuLP is installed during preparation) the PuLP
    model template are reused. The remaining variable part is the exact-count
    right-hand side and an optional MIP start.

    Solver selection:
      * ``backend="pulp", pulp_solver="cbc"`` — CBC through PuLP;
      * ``backend="pulp", pulp_solver="highs"`` — synchronous HiGHS through PuLP;
      * ``backend="highs"`` — direct highspy, required for live incumbents via
        ``progress_callback``;
      * ``backend="scipy"`` — SciPy/HiGHS compatibility backend without live
        callbacks.
    """
    if prepared is not None:
        result = _solve_prepared_rectangle_problem(
            prepared,
            n,
            initial_indices=initial_indices,
            solver=solver,
            solver_msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            require_optimal=require_optimal,
            backend=backend,
            pulp_solver=pulp_solver,
            highs_options=highs_options,
            progress_callback=progress_callback,
        )
        return _finalize_public_result_metadata(result)

    if value_matrix is None or xs is None or ys is None or rectangles is None or densities is None:
        raise ValueError(
            "Без prepared необходимо передать value_matrix, xs, ys, rectangles и densities"
        )

    normalized_backend = str(backend).strip().lower()
    normalized_pulp_solver = _normalize_pulp_solver_name(pulp_solver)
    if normalized_backend not in {"pulp", "highs", "scipy"}:
        raise ValueError("backend должен быть равен 'pulp', 'highs' или 'scipy'")
    needs_prepared_route = (
        normalized_backend == "highs"
        or (normalized_backend == "pulp" and normalized_pulp_solver == "highs")
        or progress_callback is not None
    )
    if needs_prepared_route:
        if solver is not None and normalized_backend != "pulp":
            raise ValueError("Параметр solver применим только к backend='pulp'")
        temporary_prepared = prepare_rectangle_problem(
            value_matrix=value_matrix,
            xs=xs,
            ys=ys,
            rectangles=rectangles,
            densities=densities,
            recipes=recipes,
            holds=holds,
            axis=axis,
            mosaic=mosaic,
            cover_zero_cells=cover_zero_cells,
            max_n=n,
            build_pulp_template=(normalized_backend == "pulp"),
        )
        result = _solve_prepared_rectangle_problem(
            temporary_prepared,
            n,
            initial_indices=initial_indices,
            solver=solver,
            solver_msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            require_optimal=require_optimal,
            backend=normalized_backend,
            pulp_solver=normalized_pulp_solver,
            highs_options=highs_options,
            progress_callback=progress_callback,
        )
        return _finalize_public_result_metadata(result)

    if not recipes:
        result = _select_min_density_rectangles_legacy(
            value_matrix=value_matrix,
            xs=xs,
            ys=ys,
            rectangles=rectangles,
            densities=densities,
            n=n,
            holds=holds,
            axis=axis,
            mosaic=mosaic,
            initial_indices=initial_indices,
            cover_zero_cells=cover_zero_cells,
            solver=solver,
            solver_msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            require_optimal=require_optimal,
            decompose=decompose,
            greedy_warm_start_limit=greedy_warm_start_limit,
            backend=backend,
        )
        return _finalize_public_result_metadata(result)

    result = _select_min_density_rectangles_recipes(
        value_matrix=value_matrix,
        xs=xs,
        ys=ys,
        rectangles=rectangles,
        densities=densities,
        recipes=recipes,
        n=n,
        holds=holds,
        axis=axis,
        mosaic=mosaic,
        initial_indices=initial_indices,
        cover_zero_cells=cover_zero_cells,
        solver=solver,
        solver_msg=solver_msg,
        time_limit=time_limit,
        threads=threads,
        require_optimal=require_optimal,
        decompose=decompose,
        greedy_warm_start_limit=greedy_warm_start_limit,
        backend=backend,
    )
    return _finalize_public_result_metadata(result)
