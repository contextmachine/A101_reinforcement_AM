from __future__ import annotations

from collections import defaultdict
from time import monotonic
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

__version__ = "2.1.1-fast-batch"


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


def _candidate_details(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": candidate["original_index"],
        "rectangle": candidate["rect"],
        "w": candidate["level"],
        "density": candidate["density"],
        "area": candidate["area"],
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
        "total_cost": None,
        "total_weighted_area": None,
        "total_area": None,
        "n_rectangles": None,
        "n_requested": n_requested,
        "status": status,
        "is_feasible": False,
        "is_optimal": False,
        "solver_proved_optimal": False,
        "infeasibility_proved": infeasibility_proved,
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

    warm_start = False
    if len(candidates) <= greedy_warm_start_limit:
        initial = _greedy_start(candidates, required_cells, exact_count)
        if initial is not None:
            warm_start = True
            for index, variable in enumerate(variables):
                variable.setInitialValue(1 if index in initial else 0)

    active_solver = solver
    if active_solver is None:
        active_solver = _cbc_solver(
            msg=solver_msg,
            time_limit=time_limit,
            threads=threads,
            warm_start=warm_start,
        )

    model.solve(active_solver)

    status_name = pulp.LpStatus.get(model.status, str(model.status))
    solution_code = getattr(model, "sol_status", None)
    solution_name = getattr(pulp, "LpSolution", {}).get(
        solution_code,
        str(solution_code),
    )
    optimal_solution_code = getattr(pulp, "LpSolutionOptimal", 1)
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

    return {
        "chosen_indices": chosen_indices,
        "status": status_name,
        "solution_status": solution_name,
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        "objective": (
            float(sum(candidates[index]["cost"] for index in chosen_indices))
            if feasible
            else None
        ),
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

    return {
        "chosen_indices": chosen_indices,
        "status": status_name,
        "solution_status": str(result.message),
        "proved_optimal": proved_optimal,
        "proved_infeasible": proved_infeasible,
        "feasible": feasible,
        "objective": float(result.fun) if feasible and result.fun is not None else None,
        "mip_gap": getattr(result, "mip_gap", None),
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


def select_min_density_rectangles(
    value_matrix: Sequence[Sequence[int]],
    xs: Sequence[float],
    ys: Sequence[float],
    rectangles: Iterable[tuple[int, int, int, int, int]],
    densities: Mapping[int, float],
    n: int | None = None,
    *,
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
        Положительная плотность класса ``w``. Ключи должны идти подряд
        ``0, 1, ..., N``, а значения — строго возрастать.

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
        if key < 0 or not np.isfinite(value) or value <= 0:
            raise ValueError("Некорректный ключ или значение densities")
        density[key] = value

    if not density:
        raise ValueError("densities не должен быть пустым")

    density_keys = sorted(density)
    if density_keys != list(range(density_keys[-1] + 1)):
        raise ValueError("Ключи densities должны идти подряд: 0, 1, ..., N")
    if any(density[k] >= density[k + 1] for k in density_keys[:-1]):
        raise ValueError("Плотности должны строго возрастать с увеличением класса")

    max_required = int(requirements.max()) if requirements.size else 0
    if max_required > density_keys[-1]:
        raise ValueError(
            f"В матрице есть уровень {max_required}, но максимальный класс "
            f"densities равен {density_keys[-1]}"
        )

    x_edges = np.concatenate(([0.0], np.cumsum(xs_array)))
    y_edges = np.concatenate(([0.0], np.cumsum(ys_array)))

    raw_rectangles = list(rectangles)
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

        width = float(x_edges[xmax + 1] - x_edges[xmin])
        height = float(y_edges[ymax + 1] - y_edges[ymin])
        area = width * height
        parsed_rectangles.append(
            {
                "uid": original_index,
                "original_index": original_index,
                "rect": rect,
                "level": level,
                "density": density[level],
                "area": area,
                "cost": float(area * density[level]),
            }
        )

    base_stats: dict[str, Any] = {
        "input_rectangles": len(raw_rectangles),
        "validated_candidates": len(parsed_rectangles),
        "unique_input_rectangles": len(seen_rectangles),
        "n_requested": n,
        "backend": backend,
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
    active_count = int(active_mask.sum())
    base_stats["active_cells"] = active_count

    # Если покрывать нечего, строгий n всё равно соблюдается: берутся n
    # самых дешёвых кандидатов. При n=None оптимально не выбирать ничего.
    if active_count == 0:
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

    if n == 1:
        yy, xx = np.nonzero(active_mask)
        xmin, xmax = int(xx.min()), int(xx.max())
        ymin, ymax = int(yy.min()), int(yy.max())
        min_level = int(requirements[active_mask].max())

        feasible = [
            c for c in parsed_rectangles
            if c["rect"][0] <= xmin
               and c["rect"][1] <= ymin
               and c["rect"][2] >= xmax
               and c["rect"][3] >= ymax
               and c["level"] >= min_level
        ]

        if not feasible:
            return _empty_result(
                status="Infeasible",
                n_requested=1,
                reason="Нет одного прямоугольника, покрывающего все требуемые ячейки",
                stats={**base_stats, "n1_fast_path": True},
                infeasibility_proved=True,
            )

        chosen = min(
            feasible,
            key=lambda c: (c["cost"], c["original_index"]),
        )

        return {
            "rectangles": [chosen["rect"]],
            "indices": [chosen["original_index"]],
            "details": [_candidate_details(chosen)],
            "total_cost": chosen["cost"],
            "total_weighted_area": chosen["cost"],
            "total_area": chosen["area"],
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

    cell_id = np.full((ny, nx), -1, dtype=np.int32)
    cell_id[active_mask] = np.arange(active_count, dtype=np.int32)

    levels_present = {candidate["level"] for candidate in parsed_rectangles}
    eligibility_prefix: dict[int, np.ndarray] = {}
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

        if eligible_count(candidate) == 0:
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

    candidates: list[dict[str, Any]] = []
    for group in coverage_groups.values():
        group.sort(key=lambda candidate: (candidate["cost"], candidate["original_index"]))
        if n is None:
            candidates.append(group[0])
        else:
            # При строгом n из одинаковой группы может понадобиться несколько
            # кандидатов, но никогда больше n. Оставляем n самых дешёвых.
            candidates.extend(group[: min(n, len(group))])

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
        yx = np.argwhere(active_mask)
        sample = [tuple(map(int, yx[cell])) for cell in missing[:10]]
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
                reduced.extend(group[: min(remaining_slots, len(group))])
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
                return _empty_result(
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

    details = [_candidate_details(candidate) for candidate in selected_candidates]
    total_cost = float(sum(candidate["cost"] for candidate in selected_candidates))

    return {
        "rectangles": [candidate["rect"] for candidate in selected_candidates],
        "indices": [candidate["original_index"] for candidate in selected_candidates],
        "details": details,
        "total_cost": total_cost,
        "total_weighted_area": total_cost,
        "total_area": float(sum(candidate["area"] for candidate in selected_candidates)),
        "n_rectangles": len(selected_candidates),
        "n_requested": n,
        "status": "Optimal" if all_components_optimal else "Feasible",
        "is_feasible": True,
        "is_optimal": all_components_optimal,
        "solver_proved_optimal": all_components_optimal,
        "infeasibility_proved": False,
        "reason": None,
        "stats": {
            **base_stats,
            "forced_candidates": len(forced),
            "components": components_count,
            "decomposition_used": decomposition_used,
            "strict_count_model": n is not None,
        },
        "component_statuses": component_statuses,
    }