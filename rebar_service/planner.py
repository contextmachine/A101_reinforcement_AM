from __future__ import annotations

from collections import deque
from typing import Iterable

from .models import RangeN


def _unique_ns(values: Iterable[int]) -> list[int]:
    out, seen = [], set()
    for raw in values:
        if isinstance(raw, bool) or int(raw) != raw or int(raw) < 0:
            raise ValueError("N должен содержать только неотрицательные целые")
        n = int(raw)
        if n not in seen:
            seen.add(n)
            out.append(n)
    if not out:
        raise ValueError("Список N не должен быть пустым")
    return out


def coarse_refinement_order(start: int, stop: int, coarse_step: int | None = None) -> list[int]:
    """Coarse grid first, then breadth-first midpoints of uncovered intervals.

    For 1..100 and coarse_step=10 the first passes are 10,20,...,100,
    then 5,15,25,...,95, followed by progressively finer midpoints.
    """

    if start < 0 or stop < start:
        raise ValueError("Некорректный диапазон N")
    size = stop - start + 1
    step = coarse_step or max(1, round(size / 10))
    step = max(1, int(step))

    seeds = [n for n in range(start, stop + 1) if n % step == 0]
    if stop not in seeds:
        seeds.append(stop)
    if not seeds:
        seeds = [stop]
    seeds = sorted(set(seeds))

    order = list(seeds)
    used = set(order)
    boundaries = [start - 1, *seeds]
    queue = deque((boundaries[i], boundaries[i + 1], 1) for i in range(len(boundaries) - 1))
    levels: dict[int, list[int]] = {}
    while queue:
        left, right, depth = queue.popleft()
        if right - left <= 1:
            continue
        mid = (left + right) // 2
        if mid < start:
            mid = start
        if mid >= right:
            continue
        if mid not in used:
            levels.setdefault(depth, []).append(mid)
            used.add(mid)
        queue.append((left, mid, depth + 1))
        queue.append((mid, right, depth + 1))

    for depth in sorted(levels):
        order.extend(sorted(levels[depth]))
    order.extend(n for n in range(start, stop + 1) if n not in used)
    return order



def validate_n_request_limits(value, *, max_values: int, max_n: int) -> None:
    """Reject oversized plans before materializing a full adaptive order."""

    if isinstance(value, dict):
        value = RangeN.model_validate(value)
    if isinstance(value, RangeN):
        count = value.stop - value.start + 1
        high = value.stop
    elif isinstance(value, int) and not isinstance(value, bool):
        count, high = 1, int(value)
    elif isinstance(value, list):
        count = len(value)
        high = max((int(x) for x in value), default=0)
    else:
        raise ValueError("n должен быть числом, списком или диапазоном")
    if count > int(max_values):
        raise ValueError(f"Запрошено слишком много значений N: {count} > {max_values}")
    if high > int(max_n):
        raise ValueError(f"N={high} превышает максимальный разрешённый N={max_n}")



def validate_solver_limits(solver, *, max_threads: int, max_timeout: float) -> None:
    threads = int(solver.get("threads", 1))
    timeout = float(solver.get("timeout_seconds", 120.0))
    inner = solver.get("solver_time_limit")
    if threads > int(max_threads):
        raise ValueError(f"solver.threads={threads} превышает серверный лимит {max_threads}")
    if timeout > float(max_timeout):
        raise ValueError(f"solver.timeout_seconds={timeout} превышает серверный лимит {max_timeout}")
    if inner is not None and float(inner) > timeout:
        raise ValueError("solver_time_limit не может превышать timeout_seconds")

def normalize_n_request(value) -> tuple[str, list[int], dict]:
    if isinstance(value, RangeN):
        order = coarse_refinement_order(value.start, value.stop, value.coarse_step)
        return "range", order, value.model_dump()
    if isinstance(value, int) and not isinstance(value, bool):
        return "single", _unique_ns([value]), {"value": int(value)}
    if isinstance(value, list):
        return "list", _unique_ns(value), {"values": _unique_ns(value)}
    if isinstance(value, dict):
        spec = RangeN.model_validate(value)
        order = coarse_refinement_order(spec.start, spec.stop, spec.coarse_step)
        return "range", order, spec.model_dump()
    raise ValueError("n должен быть числом, списком или диапазоном")
