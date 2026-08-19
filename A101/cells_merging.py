import numpy as np
import matplotlib.pyplot as plt

def reduce_mosaic(
    int_matrix,
    rectangles,
    target=520,
    rect_target=None,
    force_reduce=True,
    show=True
):
    a = np.asarray(int_matrix)
    R = list(map(tuple, rectangles))
    ny, nx = a.shape

    # Если отдельно не задан лимит прямоугольников,
    # используем тот же target
    if rect_target is None:
        rect_target = target

    # Если хотим гарантированно получить меньше прямоугольников
    if force_reduce and len(R) > 0:
        rect_target = min(rect_target, len(R) - 1)

    V = np.zeros((ny, nx - 1), dtype=int)
    H = np.zeros((ny - 1, nx), dtype=int)

    # Границы классов int_matrix трогать нельзя
    hardV = a[:, :-1] != a[:, 1:]
    hardH = a[:-1, :] != a[1:, :]

    supporters = {}
    redges = [[] for _ in R]

    def add(k, i):
        supporters.setdefault(k, []).append(i)
        redges[i].append(k)

        o, y, x = k
        if o == 0:
            V[y, x] += 1
        else:
            H[y, x] += 1

    # ------------------------------------------------------------
    # Строим границы всех прямоугольников
    # ------------------------------------------------------------
    for i, (x0, y0, x1, y1, *_) in enumerate(R):

        if x0:
            for y in range(y0, y1 + 1):
                add((0, y, x0 - 1), i)

        if x1 < nx - 1:
            for y in range(y0, y1 + 1):
                add((0, y, x1), i)

        if y0:
            for x in range(x0, x1 + 1):
                add((1, y0 - 1, x), i)

        if y1 < ny - 1:
            for x in range(x0, x1 + 1):
                add((1, y1, x), i)

    active = np.ones(len(R), dtype=bool)

    # ------------------------------------------------------------
    # Connected components
    # ------------------------------------------------------------
    def components(v=V, h=H):

        p = np.arange(ny * nx)

        def find(i):
            while p[i] != i:
                p[i] = p[p[i]]
                i = p[i]
            return i

        def union(i, j):
            i = find(i)
            j = find(j)

            if i != j:
                p[j] = i

        # Горизонтальные соседи
        for y in range(ny):
            for x in range(nx - 1):
                if not hardV[y, x] and v[y, x] == 0:
                    union(
                        y * nx + x,
                        y * nx + x + 1
                    )

        # Вертикальные соседи
        for y in range(ny - 1):
            for x in range(nx):
                if not hardH[y, x] and h[y, x] == 0:
                    union(
                        y * nx + x,
                        (y + 1) * nx + x
                    )

        roots = np.array([
            find(i)
            for i in range(ny * nx)
        ])

        _, z = np.unique(
            roots,
            return_inverse=True
        )

        z = z.reshape(ny, nx)

        return z, z.max() + 1

    # ------------------------------------------------------------
    # Удаление прямоугольника
    # ------------------------------------------------------------
    def deactivate(i):

        if not active[i]:
            return

        active[i] = False

        for o, y, x in redges[i]:
            if o == 0:
                V[y, x] -= 1
            else:
                H[y, x] -= 1

    # ------------------------------------------------------------
    # Можно ли удалить прямоугольник,
    # совершенно не изменив набор активных границ?
    # ------------------------------------------------------------
    def is_redundant(i):

        if not active[i]:
            return False

        for o, y, x in redges[i]:

            if o == 0:
                # hardV всё равно останется границей
                if hardV[y, x]:
                    continue

                # Если это единственный прямоугольник,
                # поддерживающий границу — удалять нельзя
                if V[y, x] <= 1:
                    return False

            else:
                if hardH[y, x]:
                    continue

                if H[y, x] <= 1:
                    return False

        return True

    # ------------------------------------------------------------
    # Исходная мозаика
    # ------------------------------------------------------------
    mosaic, n0 = components()

    # Абсолютный минимум:
    # только границы классов int_matrix
    _, minimum = components(
        np.zeros_like(V),
        np.zeros_like(H)
    )

    # ============================================================
    # ШАГ 1.
    # Удаляем избыточные прямоугольники,
    # НЕ меняя мозаику вообще
    # ============================================================

    safe_removed = 0

    changed = True

    while changed:
        changed = False

        # Большие сначала можно заменить на reverse=False/True
        # в зависимости от задачи
        ids = np.flatnonzero(active)

        for i in ids:

            # Уже достаточно прямоугольников
            if active.sum() <= rect_target:
                break

            if is_redundant(i):
                deactivate(i)
                safe_removed += 1
                changed = True

    mosaic, ncur = components()

    # ============================================================
    # ШАГ 2.
    #
    # В старой версии было:
    #
    #     while mosaic.max()+1 > target:
    #
    # Поэтому если n0 <= target, ничего не происходило.
    #
    # Теперь продолжаем также, если прямоугольников
    # больше rect_target.
    # ============================================================

    while (
        ncur > target
        or active.sum() > rect_target
    ):

        candidates = []

        # Вертикальные границы
        y, x = np.where(
            (~hardV) & (V > 0)
        )

        for Y, X in zip(y, x):
            candidates.append(
                (V[Y, X], 0, Y, X)
            )

        # Горизонтальные границы
        y, x = np.where(
            (~hardH) & (H > 0)
        )

        for Y, X in zip(y, x):
            candidates.append(
                (H[Y, X], 1, Y, X)
            )

        if not candidates:
            break

        # Самая слабая граница:
        # чем меньше прямоугольников её поддерживает,
        # тем раньше она исчезает
        _, o, y, x = min(candidates)

        ids = [
            i
            for i in supporters.get((o, y, x), [])
            if active[i]
        ]

        if not ids:
            # На всякий случай
            if o == 0:
                V[y, x] = 0
            else:
                H[y, x] = 0
            continue

        # Удаляем все прямоугольники,
        # создающие выбранную границу
        for i in ids:
            deactivate(i)

        mosaic, ncur = components()

    # ------------------------------------------------------------
    # Результат
    # ------------------------------------------------------------

    active_ids = np.flatnonzero(active)
    removed = np.flatnonzero(~active)

    result = [R[i] for i in active_ids]

    n1 = mosaic.max() + 1

    print(f"Мозаика: {n0} → {n1}")
    print(
        f"Прямоугольники: "
        f"{len(R)} → {len(result)} "
        f"(удалено {len(removed)})"
    )
    print(
        f"Из них удалено без изменения мозаики: "
        f"{safe_removed}"
    )
    print(
        f"Минимум без смешивания классов: "
        f"{minimum}"
    )

    if show:

        plt.figure(figsize=(12, 8))

        plt.imshow(
            a,
            origin="lower",
            interpolation="none"
        )

        ax = plt.gca()

        for i in active_ids:

            x0, y0, x1, y1, *_ = R[i]

            ax.add_patch(
                plt.Rectangle(
                    (x0 - .5, y0 - .5),
                    x1 - x0 + 1,
                    y1 - y0 + 1,
                    fill=False,
                    lw=.3,
                    alpha=.25
                )
            )

        plt.title(
            f"Мозаика: {n0} → {n1}; "
            f"прямоугольники: {len(R)} → {len(result)}"
        )

        plt.show()

    return result, mosaic, removed