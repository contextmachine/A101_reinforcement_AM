from dataclasses import dataclass
from itertools import accumulate
import pulp
import random
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, Normalize


def generate_grid_example(
    m,
    one_probability=0.5,
    min_cell_size=0.5,
    max_cell_size=2.0,
    seed=None,
):
    """
    Генерирует пример нерегулярной сетки M × M.

    Параметры:
        m               — количество строк и столбцов;
        one_probability — вероятность единицы в матрице;
        min_cell_size   — минимальный размер ячейки;
        max_cell_size   — максимальный размер ячейки;
        seed            — seed для воспроизводимости.

    Возвращает:
        mask    — бинарная матрица M × M;
        x_sizes — размеры столбцов;
        y_sizes — размеры строк.
    """

    if m <= 0:
        raise ValueError("m должно быть положительным")

    if not 0 <= one_probability <= 1:
        raise ValueError("one_probability должно лежать в диапазоне [0, 1]")

    if min_cell_size <= 0 or min_cell_size > max_cell_size:
        raise ValueError("Некорректный диапазон размеров ячеек")

    rng = random.Random(seed)

    x_sizes = [
        round(rng.uniform(min_cell_size, max_cell_size), 3)
        for _ in range(m)
    ]

    y_sizes = [
        round(rng.uniform(min_cell_size, max_cell_size), 3)
        for _ in range(m)
    ]

    mask = [
        [
            int(rng.random() < one_probability)
            for _ in range(m)
        ]
        for _ in range(m)
    ]

    # Гарантируем наличие хотя бы одной единицы.
    if not any(any(row) for row in mask):
        mask[rng.randrange(m)][rng.randrange(m)] = 1

    return mask, x_sizes, y_sizes


def make_cbc_solver(
        *,
        msg=False,
        time_limit=None,
):
    """
    Совместимость со старыми и новыми версиями PuLP.
    """

    solver_class = getattr(
        pulp,
        "PULP_CBC_CMD",
        None,
    )

    if solver_class is None:
        solver_class = pulp.COIN_CMD

    return solver_class(
        msg=msg,
        timeLimit=time_limit,
        gapRel=0.0,
        gapAbs=0.0,
    )


@dataclass
class RectangleCoverModel:
    model: pulp.LpProblem
    selected: dict
    candidates: list
    rectangle_count: pulp.LpVariable
    area_expression: pulp.LpAffineExpression
    minimum_possible_area: float
    x_edges: list
    y_edges: list

    def _default_solver(self):
        return make_cbc_solver(msg=False)

    def solve(
            self,
            n_rectangles,
            *,
            solver=None,
            require_optimal=False,
            area_tolerance=1e-7,
    ):
        """
        Минимизирует площадь при фиксированном количестве боксов.

        Возвращает как найденное решение, так и информацию
        о том, доказана ли его оптимальность.
        """

        n_rectangles = int(n_rectangles)

        if not 0 <= n_rectangles <= len(self.candidates):
            raise ValueError(
                "Некорректное количество прямоугольников"
            )

        # Фиксируем количество прямоугольников.
        self.rectangle_count.lowBound = n_rectangles
        self.rectangle_count.upBound = n_rectangles

        # Очищаем значения предыдущего запуска.
        # Иначе при неудачном следующем solve старые значения
        # могут выглядеть как новое решение.
        for variable in self.selected.values():
            variable.varValue = None

        self.rectangle_count.varValue = None

        if solver is None:
            solver = self._default_solver()

        # На случай, если цель модели ранее менялась.
        self.model.sense = pulp.LpMinimize
        self.model.setObjective(self.area_expression)

        self.model.solve(solver)

        status_code = self.model.status
        status_name = pulp.LpStatus.get(
            status_code,
            str(status_code),
        )

        # В современных версиях PuLP это более точный
        # статус именно найденного решения.
        solution_status_code = getattr(
            self.model,
            "sol_status",
            None,
        )

        solution_status_name = getattr(
            pulp,
            "LpSolution",
            {},
        ).get(
            solution_status_code,
            "Недоступен",
        )

        optimal_solution_code = getattr(
            pulp,
            "LpSolutionOptimal",
            1,
        )

        feasible_solution_code = getattr(
            pulp,
            "LpSolutionIntegerFeasible",
            2,
        )

        # Определяем, существует ли текущее допустимое решение.
        if solution_status_code is not None:
            has_solution = solution_status_code in {
                optimal_solution_code,
                feasible_solution_code,
            }
        else:
            # Резервный вариант для старых версий PuLP.
            has_solution = (
                    status_code == pulp.LpStatusOptimal
                    or any(
                variable.varValue is not None
                for variable in self.selected.values()
            )
            )

        # Строгая проверка: решатель сообщил,
        # что глобальный оптимум доказан.
        solver_proved_optimal = (
                status_code == pulp.LpStatusOptimal
                and (
                        solution_status_code is None
                        or solution_status_code
                        == optimal_solution_code
                )
        )

        rectangles = None
        area = None

        if has_solution:
            area = pulp.value(self.area_expression)
            rectangles = []

            for k, variable in self.selected.items():
                if (
                        variable.varValue is not None
                        and variable.varValue > 0.5
                ):
                    x1, y1, x2, y2, _ = self.candidates[k]

                    rectangles.append((
                        self.x_edges[x1],
                        self.y_edges[y1],
                        self.x_edges[x2],
                        self.y_edges[y2],
                    ))

        # Если площадь совпала с суммарной площадью единиц,
        # достигнута теоретическая нижняя граница.
        reached_lower_bound = (
                area is not None
                and area
                <= self.minimum_possible_area + area_tolerance
        )

        # В этом случае оптимальность доказана математически,
        # даже если CBC остановился до собственного доказательства.
        is_optimal = (
                solver_proved_optimal
                or reached_lower_bound
        )

        if solver_proved_optimal and reached_lower_bound:
            optimality_reason = (
                "solver_and_theoretical_lower_bound"
            )
        elif solver_proved_optimal:
            optimality_reason = "solver"
        elif reached_lower_bound:
            optimality_reason = (
                "theoretical_lower_bound"
            )
        else:
            optimality_reason = None

        result = {
            "n_rectangles": n_rectangles,
            "rectangles": rectangles,
            "area": area,
            "has_solution": has_solution,

            # Главный итоговый флаг.
            "is_optimal": is_optimal,

            # Оптимальность доказана именно CBC.
            "solver_proved_optimal": (
                solver_proved_optimal
            ),

            # Достигнута минимально возможная площадь.
            "reached_lower_bound": (
                reached_lower_bound
            ),

            "optimality_reason": optimality_reason,
            "status": status_name,
            "solution_status": solution_status_name,
        }

        if require_optimal and not is_optimal:
            raise RuntimeError(
                f"Оптимум для N={n_rectangles} "
                "не доказан: "
                f"status={status_name}, "
                f"solution_status={solution_status_name}"
            )

        return result

    def solve_many(
            self,
            n_values,
            *,
            solver=None,
            stop_on_exact_cover=True,
            area_tolerance=1e-7,
            require_optimal=False,
    ):
        """
        Решает задачи для нескольких значений N.

        Значения сортируются, дубликаты удаляются.
        После достижения минимально возможной площади
        дальнейший перебор прекращается.
        """

        if solver is None:
            solver = self._default_solver()

        values = sorted({
            int(n)
            for n in n_values
            if int(n) >= 0
        })

        solutions = {}
        skipped = {}
        saturation_count = None

        for n in values:
            if n > len(self.candidates):
                skipped[n] = {
                    "reason": (
                        "N больше числа допустимых "
                        "прямоугольников-кандидатов"
                    ),
                    "status": None,
                    "solution_status": None,
                }
                continue

            result = self.solve(
                n_rectangles=n,
                solver=solver,
                require_optimal=require_optimal,
                area_tolerance=area_tolerance,
            )

            if not result["has_solution"]:
                skipped[n] = {
                    "reason": (
                        "Допустимое решение не найдено"
                    ),
                    "status": result["status"],
                    "solution_status": (
                        result["solution_status"]
                    ),
                }
                continue

            solutions[n] = result

            # Если достигнута теоретически минимальная площадь,
            # дальнейшее увеличение числа боксов уже не улучшит цель.
            if (
                    stop_on_exact_cover
                    and result["reached_lower_bound"]
            ):
                saturation_count = n
                break

        return {
            "minimum_possible_area": (
                self.minimum_possible_area
            ),
            "saturation_count": saturation_count,
            "solutions": solutions,
            "skipped": skipped,
        }

def prepare_rectangle_model(
        mask,
        x_sizes,
        y_sizes,
        min_width=0.0,
        min_height=0.0,
        area_mode="cells",
):

    ny = len(y_sizes)
    nx = len(x_sizes)

    if len(mask) != ny or any(len(row) != nx for row in mask):
        raise ValueError(
            "Размер mask должен быть len(y_sizes) × len(x_sizes)"
        )

    if area_mode not in {"cells", "physical"}:
        raise ValueError(
            "area_mode должен быть 'cells' или 'physical'"
        )

    if area_mode == "cells":
        minimum_possible_area = sum(
            int(bool(mask[y][x]))
            for y in range(ny)
            for x in range(nx)
        )
    else:
        minimum_possible_area = sum(
            x_sizes[x] * y_sizes[y]
            for y in range(ny)
            for x in range(nx)
            if mask[y][x]
        )

    x_edges = [0.0, *accumulate(x_sizes)]
    y_edges = [0.0, *accumulate(y_sizes)]

    # Префиксные суммы единичных ячеек.
    prefix = [[0] * (nx + 1) for _ in range(ny + 1)]

    for y in range(ny):
        for x in range(nx):
            prefix[y + 1][x + 1] = (
                    int(bool(mask[y][x]))
                    + prefix[y][x + 1]
                    + prefix[y + 1][x]
                    - prefix[y][x]
            )

    def ones_in_box(x1, y1, x2, y2):
        return (
                prefix[y2][x2]
                - prefix[y1][x2]
                - prefix[y2][x1]
                + prefix[y1][x1]
        )

    x_spans = [
        (x1, x2)
        for x1 in range(nx)
        for x2 in range(x1 + 1, nx + 1)
        if x_edges[x2] - x_edges[x1] >= min_width - 1e-9
    ]

    y_spans = [
        (y1, y2)
        for y1 in range(ny)
        for y2 in range(y1 + 1, ny + 1)
        if y_edges[y2] - y_edges[y1] >= min_height - 1e-9
    ]

    candidates = []
    candidates_by_cell = [
        [[] for _ in range(nx)]
        for _ in range(ny)
    ]

    for x1, x2 in x_spans:
        for y1, y2 in y_spans:
            # Каждый прямоугольник содержит хотя бы одну единицу.
            if ones_in_box(x1, y1, x2, y2) == 0:
                continue

            if area_mode == "cells":
                cost = (x2 - x1) * (y2 - y1)
            else:
                cost = (
                        (x_edges[x2] - x_edges[x1])
                        * (y_edges[y2] - y_edges[y1])
                )

            k = len(candidates)
            candidates.append((x1, y1, x2, y2, cost))

            for y in range(y1, y2):
                for x in range(x1, x2):
                    candidates_by_cell[y][x].append(k)

    model = pulp.LpProblem(
        "grid_rectangle_cover",
        pulp.LpMinimize,
    )

    selected = pulp.LpVariable.dicts(
        "selected",
        range(len(candidates)),
        cat=pulp.LpBinary,
    )

    area_expression = pulp.lpSum(
        candidates[k][4] * selected[k]
        for k in range(len(candidates))
    )

    model += area_expression

    # Эта переменная будет фиксироваться перед каждым solve().
    rectangle_count = pulp.LpVariable(
        "rectangle_count",
        lowBound=0,
        upBound=len(candidates),
        cat=pulp.LpInteger,
    )

    model += (
        pulp.lpSum(selected.values()) == rectangle_count,
        "number_of_rectangles",
    )

    for y in range(ny):
        for x in range(nx):
            ids = candidates_by_cell[y][x]

            if mask[y][x]:
                if not ids:
                    raise ValueError(
                        f"Ячейку ({x}, {y}) невозможно покрыть"
                    )

                model += (
                    pulp.lpSum(selected[k] for k in ids) == 1,
                    f"cover_{x}_{y}",
                )

            elif ids:
                model += (
                    pulp.lpSum(selected[k] for k in ids) <= 1,
                    f"no_overlap_{x}_{y}",
                )

    return RectangleCoverModel(
        model=model,
        selected=selected,
        candidates=candidates,
        rectangle_count=rectangle_count,
        area_expression=area_expression,
        minimum_possible_area=minimum_possible_area,
        x_edges=x_edges,
        y_edges=y_edges
    )


def find_minimum_rectangle_cover(
    mask,
    x_sizes=None,
    y_sizes=None,
    *,
    min_width=0.0,
    min_height=0.0,
    allow_cover_zeros=False,
    area_mode="cells",
    solver_msg=False,
):
    """
    Находит минимальное число непересекающихся прямоугольников,
    покрывающих все единичные ячейки mask.

    Параметры
    ----------
    mask : list[list[int]]
        Бинарная матрица mask[y][x].

    x_sizes, y_sizes : list[float] | None
        Размеры столбцов и строк.
        Если не переданы, все ячейки считаются единичными.

    min_width, min_height : float
        Минимальные физические размеры прямоугольника.

    allow_cover_zeros : bool
        False:
            прямоугольники могут покрывать только единичные ячейки.

        True:
            прямоугольники могут также захватывать нулевые ячейки.
            В этом случае минимальное число обычно равно 1.

    area_mode : {"cells", "physical"}
        Способ подсчёта площади для второго этапа оптимизации:
        "cells"    — число покрытых ячеек;
        "physical" — физическая площадь.

    Возвращает
    ----------
    dict:
        {
            "n_rectangles": минимальное число прямоугольников,
            "rectangles": [(x1, y1, x2, y2), ...],
            "area": минимальная площадь среди решений
                    с минимальным числом прямоугольников
        }
    """

    ny = len(mask)

    if ny == 0:
        return {
            "n_rectangles": 0,
            "rectangles": [],
            "area": 0.0,
        }

    nx = len(mask[0])

    if nx == 0:
        return {
            "n_rectangles": 0,
            "rectangles": [],
            "area": 0.0,
        }

    if any(len(row) != nx for row in mask):
        raise ValueError("Все строки mask должны иметь одинаковую длину")

    if any(value not in (0, 1, False, True) for row in mask for value in row):
        raise ValueError("mask должна быть бинарной")

    if x_sizes is None:
        x_sizes = [1.0] * nx

    if y_sizes is None:
        y_sizes = [1.0] * ny

    if len(x_sizes) != nx:
        raise ValueError("len(x_sizes) должен совпадать с числом столбцов")

    if len(y_sizes) != ny:
        raise ValueError("len(y_sizes) должен совпадать с числом строк")

    if any(size <= 0 for size in x_sizes + y_sizes):
        raise ValueError("Размеры ячеек должны быть положительными")

    if area_mode not in {"cells", "physical"}:
        raise ValueError(
            "area_mode должен быть равен 'cells' или 'physical'"
        )

    number_of_ones = sum(
        int(bool(value))
        for row in mask
        for value in row
    )

    if number_of_ones == 0:
        return {
            "n_rectangles": 0,
            "rectangles": [],
            "area": 0.0,
        }

    x_edges = [0.0, *accumulate(x_sizes)]
    y_edges = [0.0, *accumulate(y_sizes)]

    # Префиксные суммы для быстрого подсчёта единиц
    # внутри любого прямоугольника.
    prefix = [[0] * (nx + 1) for _ in range(ny + 1)]

    for y in range(ny):
        for x in range(nx):
            prefix[y + 1][x + 1] = (
                int(bool(mask[y][x]))
                + prefix[y][x + 1]
                + prefix[y + 1][x]
                - prefix[y][x]
            )

    def ones_in_box(x1, y1, x2, y2):
        return (
            prefix[y2][x2]
            - prefix[y1][x2]
            - prefix[y2][x1]
            + prefix[y1][x1]
        )

    candidates = []

    candidates_by_cell = [
        [[] for _ in range(nx)]
        for _ in range(ny)
    ]

    # Генерируем все допустимые прямоугольники.
    for x1 in range(nx):
        for x2 in range(x1 + 1, nx + 1):
            physical_width = x_edges[x2] - x_edges[x1]

            if physical_width < min_width - 1e-9:
                continue

            for y1 in range(ny):
                for y2 in range(y1 + 1, ny + 1):
                    physical_height = y_edges[y2] - y_edges[y1]

                    if physical_height < min_height - 1e-9:
                        continue

                    ones_count = ones_in_box(x1, y1, x2, y2)

                    # Пустой прямоугольник никогда не нужен.
                    if ones_count == 0:
                        continue

                    cell_count = (x2 - x1) * (y2 - y1)

                    if not allow_cover_zeros:
                        # Допускаются только полностью единичные боксы.
                        if ones_count != cell_count:
                            continue

                    if area_mode == "cells":
                        area = cell_count
                    else:
                        area = physical_width * physical_height

                    candidate_id = len(candidates)

                    candidates.append({
                        "indices": (x1, y1, x2, y2),
                        "coordinates": (
                            x_edges[x1],
                            y_edges[y1],
                            x_edges[x2],
                            y_edges[y2],
                        ),
                        "area": area,
                    })

                    for y in range(y1, y2):
                        for x in range(x1, x2):
                            candidates_by_cell[y][x].append(candidate_id)

    if not candidates:
        raise ValueError(
            "Нет допустимых прямоугольников с заданными ограничениями"
        )

    # Проверяем, что каждую единицу вообще можно покрыть.
    for y in range(ny):
        for x in range(nx):
            if mask[y][x] and not candidates_by_cell[y][x]:
                raise ValueError(
                    f"Ячейку ({x}, {y}) невозможно покрыть "
                    "с заданными ограничениями"
                )

    model = pulp.LpProblem(
        "minimum_rectangle_cover",
        pulp.LpMinimize,
    )

    selected = pulp.LpVariable.dicts(
        "selected",
        range(len(candidates)),
        cat=pulp.LpBinary,
    )

    count_expression = pulp.lpSum(
        selected[k]
        for k in range(len(candidates))
    )

    area_expression = pulp.lpSum(
        candidates[k]["area"] * selected[k]
        for k in range(len(candidates))
    )

    # Каждая единичная ячейка покрывается ровно один раз.
    for y in range(ny):
        for x in range(nx):
            candidate_ids = candidates_by_cell[y][x]

            if mask[y][x]:
                model += (
                    pulp.lpSum(
                        selected[k]
                        for k in candidate_ids
                    ) == 1,
                    f"cover_one_{x}_{y}",
                )

            elif allow_cover_zeros and candidate_ids:
                # Запрещаем пересечение прямоугольников
                # на нулевых ячейках.
                model += (
                    pulp.lpSum(
                        selected[k]
                        for k in candidate_ids
                    ) <= 1,
                    f"no_overlap_zero_{x}_{y}",
                )

    solver = pulp.PULP_CBC_CMD(
        msg=solver_msg,
        gapRel=0.0,
        gapAbs=0.0,
    )

    # Этап 1: минимизируем число прямоугольников.
    model.setObjective(count_expression)
    model.solve(solver)

    if model.status != pulp.LpStatusOptimal:
        raise RuntimeError(
            "Не удалось доказать оптимальное минимальное число "
            f"прямоугольников. Статус: {pulp.LpStatus[model.status]}"
        )

    min_count = round(pulp.value(count_expression))

    # Фиксируем найденное минимальное количество.
    model += (
        count_expression == min_count,
        "fix_minimum_rectangle_count",
    )

    # Этап 2: среди решений с минимальным числом боксов
    # минимизируем площадь.
    model.setObjective(area_expression)
    model.solve(solver)

    if model.status != pulp.LpStatusOptimal:
        raise RuntimeError(
            "Не удалось доказать оптимальность по площади. "
            f"Статус: {pulp.LpStatus[model.status]}"
        )

    rectangles = [
        candidates[k]["coordinates"]
        for k in range(len(candidates))
        if pulp.value(selected[k]) > 0.5
    ]

    return {
        "n_rectangles": min_count,
        "rectangles": rectangles,
        "area": pulp.value(area_expression),
    }

def visualize_rectangles(
    matrix,
    x_sizes,
    y_sizes,
    rectangles=None,
    *,
    show_values=False,
    figsize=(10, 8),
    ax=None,
):
    """
    Визуализирует нерегулярную прямоугольную сетку.

    Параметры
    ----------
    matrix
        Матрица значений размером len(y_sizes) × len(x_sizes).

        Если матрица бинарная (содержит только 0/1 или False/True),
        то отображается как раньше:
            0 -> белый
            1 -> светло-серый

        Иначе используется цветовая шкала:
            0 -> белый
            максимальное значение -> максимальный цвет шкалы.

    x_sizes
        Ширины столбцов.

    y_sizes
        Высоты строк.

    rectangles
        Список прямоугольников:
            [(x1, y1, x2, y2), ...]

    show_values
        Показывать значения внутри ячеек.

    figsize
        Размер рисунка.

    ax
        Существующий matplotlib Axes.

    Возвращает
    ----------
    fig, ax
    """

    ny = len(y_sizes)
    nx = len(x_sizes)

    matrix = np.asarray(matrix)

    if matrix.shape != (ny, nx):
        raise ValueError(
            "Размер matrix должен быть len(y_sizes) × len(x_sizes)"
        )

    if any(size <= 0 for size in x_sizes):
        raise ValueError("Все элементы x_sizes должны быть положительными")

    if any(size <= 0 for size in y_sizes):
        raise ValueError("Все элементы y_sizes должны быть положительными")

    rectangles = rectangles or []

    x_edges = [0.0, *accumulate(x_sizes)]
    y_edges = [0.0, *accumulate(y_sizes)]

    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.figure

    # Определяем, бинарная ли матрица.
    unique = np.unique(matrix)
    is_binary = np.all(np.isin(unique, [0, 1]))

    if not is_binary:
        vmax = float(matrix.max())

        cmap = LinearSegmentedColormap.from_list(
            "white_to_viridis",
            ["white", *plt.cm.viridis(np.linspace(0, 1, 255))]
        )

        norm = Normalize(vmin=0, vmax=max(vmax, 1e-12))

    # Рисуем ячейки.
    for y in range(ny):
        for x in range(nx):
            x1 = x_edges[x]
            y1 = y_edges[y]

            width = x_sizes[x]
            height = y_sizes[y]

            value = matrix[y, x]

            if is_binary:
                facecolor = "lightgray" if value else "white"
            else:
                facecolor = cmap(norm(value))

            cell = Rectangle(
                (x1, y1),
                width,
                height,
                facecolor=facecolor,
                edgecolor="black",
                linewidth=0.8,
            )

            ax.add_patch(cell)

            if show_values:
                ax.text(
                    x1 + width / 2,
                    y1 + height / 2,
                    str(value),
                    ha="center",
                    va="center",
                    fontsize=9,
                )

    # Цветовая шкала.
    if not is_binary:
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        fig.colorbar(sm, ax=ax, label="Value")

        # Обводим прямоугольники.
        colors = plt.get_cmap("tab10")

        # соответствие group -> цвет
        groups = sorted({r[4] for r in rectangles if len(r) == 5})
        group_colors = {g: colors(i % 10) for i, g in enumerate(groups)}

        for index, rect in enumerate(rectangles):

            if len(rect) == 4:
                x1, y1, x2, y2 = rect
                color = colors(index % 10)

            elif len(rect) == 5:
                x1, y1, x2, y2, group = rect
                color = group_colors[group]

            else:
                raise ValueError(
                    "Прямоугольник должен содержать 4 или 5 элементов."
                )

            if x2 <= x1 or y2 <= y1:
                raise ValueError(
                    f"Некорректный прямоугольник №{index + 1}: {rect}"
                )

            ax.add_patch(Rectangle(
                (x1, y1),
                x2 - x1,
                y2 - y1,
                fill=False,
                edgecolor=color,
                linewidth=3,
                zorder=10,
            ))

            ax.text(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                str(index + 1),
                ha="center",
                va="center",
                color=color,
                fontweight="bold",
                fontsize=12,
                zorder=11,
            )

    ax.set_xlim(x_edges[0], x_edges[-1])
    ax.set_ylim(y_edges[0], y_edges[-1])

    ax.set_aspect("equal", adjustable="box")

    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.set_title("Визуализация сетки")

    ax.set_xticks(x_edges)
    ax.set_yticks(y_edges)

    fig.tight_layout()

    return fig, ax

'''
mask, x_sizes, y_sizes = generate_grid_example(20)

prepared = prepare_rectangle_model(
    mask=mask,
    x_sizes=x_sizes,
    y_sizes=y_sizes,
    area_mode="cells",
)

solver = make_cbc_solver(
    msg=False,
    time_limit=None,
)

results = prepared.solve_many(
    [1, 2, 3],
    solver=solver,
    stop_on_exact_cover=True,
    require_optimal=True,
)

for n, result in results["solutions"].items():
    print(f"\nN={n}")
    print("Площадь:", result["area"])
    print("Прямоугольники:", result["rectangles"])
    print("Есть решение:", result["has_solution"])
    print("Оптимум:", result["is_optimal"])
    print(
        "Оптимум доказан CBC:",
        result["solver_proved_optimal"],
    )
    print(
        "Достигнута нижняя граница:",
        result["reached_lower_bound"],
    )
    print(
        "Причина оптимальности:",
        result["optimality_reason"],
    )
    print("Статус модели:", result["status"])
    print(
        "Статус решения:",
        result["solution_status"],
    )'''
