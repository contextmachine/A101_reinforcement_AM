"""Exact layer-mask rectangle counts used as a *policy* scheduling limit.

This is not a proof that increasing N cannot improve the physical layout mass.
The bound has the specific mask-cover meaning agreed in the API contract.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix


class MaxNEstimationError(RuntimeError):
    """The optimization did not prove a result; this is NOT infeasibility."""


def _normalized_recipes(recipes):
    return {int(k): tuple(int(v) for v in values) for k, values in dict(recipes or {}).items()}


def _expand_leaves(cls, recipes, cache, stack=()):
    cls = int(cls)
    if cls <= 0:
        return ()
    if cls in cache:
        return cache[cls]
    if cls in stack:
        raise ValueError("cyclic reinforcement recipe")
    parts = recipes.get(cls)
    cache[cls] = ((cls,) if parts is None else tuple(
        leaf for part in parts for leaf in _expand_leaves(part, recipes, cache, (*stack, cls))
    ))
    return cache[cls]


def maximal_mask_rectangles(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Enumerate ALL maximal axis-aligned all-ones rectangles, inclusive indexes.

    Row-band intersections: O(min(ny,nx)^2 * max(ny,nx)), no greedy deletion.
    Maximal rectangles suffice for minimum set cover with overlaps: expanding an
    all-ones rectangle can only add covered required cells, at the same unit cost.
    """
    a = np.asarray(mask, dtype=bool)
    if a.ndim != 2:
        raise ValueError("mask must be two-dimensional")
    if not a.any():
        return []
    transposed = a.shape[0] > a.shape[1]
    if transposed:
        a = a.T
    ny, nx = a.shape
    if a.all():
        return [(0, 0, ny - 1, nx - 1)] if transposed else [(0, 0, nx - 1, ny - 1)]
    found = []
    for top in range(ny):
        columns = np.ones(nx, dtype=bool)
        for bottom in range(top, ny):
            columns &= a[bottom]
            if not columns.any():
                break
            edges = np.diff(np.r_[False, columns, False].astype(np.int8))
            for left, stop in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
                if top > 0 and a[top - 1, left:stop].all():
                    continue
                if bottom + 1 < ny and a[bottom + 1, left:stop].all():
                    continue
                rect = (int(left), top, int(stop - 1), bottom)
                found.append((top, int(left), bottom, int(stop - 1)) if transposed else rect)
    return found


def minimum_rectangle_cover(
    mask: np.ndarray, rectangles: Sequence[Sequence[int]] | None = None,
) -> dict[str, Any]:
    """Prove minimum cardinality; never call a time limit mathematical infeasibility."""
    required = np.asarray(mask, dtype=bool)
    if required.ndim != 2:
        raise ValueError("mask must be two-dimensional")
    cells = np.flatnonzero(required.ravel())
    if not len(cells):
        return {"feasible": True, "optimal": True, "count": 0, "chosen_indices": []}
    ny, nx = required.shape
    supplied = maximal_mask_rectangles(required) if rectangles is None else list(rectangles)
    candidates = []
    for source_index, rect in enumerate(supplied):
        x0, y0, x1, y1 = map(int, rect[:4])
        if not (0 <= x0 <= x1 < nx and 0 <= y0 <= y1 < ny):
            continue
        if not required[y0:y1 + 1, x0:x1 + 1].all():
            continue
        covered = [y * nx + x for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        candidates.append((source_index, covered))
    if not candidates:
        return {"feasible": False, "count": None, "chosen_indices": [], "reason": "no_candidates"}
    row_for_cell = {int(cell): row for row, cell in enumerate(cells)}
    rows, cols = [], []
    for col, (_, covered) in enumerate(candidates):
        rows.extend(row_for_cell[cell] for cell in covered)
        cols.extend([col] * len(covered))
    matrix = csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(cells), len(candidates)))
    if np.any(np.asarray(matrix.sum(axis=1)).ravel() == 0):
        return {"feasible": False, "count": None, "chosen_indices": [], "reason": "uncovered_cells"}
    result = milp(
        c=np.ones(len(candidates)), integrality=np.ones(len(candidates), dtype=np.int8),
        bounds=Bounds(np.zeros(len(candidates)), np.ones(len(candidates))),
        constraints=LinearConstraint(matrix, np.ones(len(cells)), np.full(len(cells), np.inf)),
        options={"disp": False, "mip_rel_gap": 0.0},
    )
    if int(result.status) == 2:
        return {"feasible": False, "count": None, "chosen_indices": [], "reason": "infeasible"}
    if int(result.status) != 0 or result.x is None:
        raise MaxNEstimationError(f"Minimum rectangle cover not proved: {result.message}")
    selected = np.asarray(result.x) > 0.5
    if np.any(np.asarray(matrix @ selected).ravel() < 1):
        raise MaxNEstimationError("MILP returned an invalid mask cover")
    chosen = [int(candidates[int(i)][0]) for i in np.flatnonzero(selected)]
    return {"feasible": True, "optimal": True, "count": len(chosen), "chosen_indices": chosen}


def estimate_max_useful_n(
    component_problem: Mapping[str, Any], *,
    recipes: Mapping[Any, Sequence[Any]] | None = None, hard_cap: int = 100,
) -> dict[str, Any]:
    matrix = np.asarray(component_problem.get("work_matrix"), dtype=np.int32)
    if matrix.ndim != 2:
        raise ValueError("component problem has no two-dimensional work_matrix")
    physical = np.asarray(component_problem.get("work_physical_mask", np.ones(matrix.shape)), dtype=bool)
    if physical.shape != matrix.shape:
        raise ValueError("work_physical_mask shape does not match work_matrix")
    cap = max(0, int(hard_cap))
    if np.any((matrix > 0) & ~physical):
        return {"feasible": False, "max_useful_n": 0, "layers": [], "capped": False,
                "reason": "required_cells_intersect_physical_void"}
    normalized = _normalized_recipes(recipes)
    cache = {}
    counts = {int(c): Counter(_expand_leaves(int(c), normalized, cache)) for c in np.unique(matrix) if c > 0}
    primitives = sorted({leaf for value in counts.values() for leaf in value})
    layers, total, solved_masks = [], 0, {}
    for primitive in primitives:
        for occurrence in range(1, max(v.get(primitive, 0) for v in counts.values()) + 1):
            mask = np.isin(matrix, [c for c, value in counts.items() if value.get(primitive, 0) >= occurrence])
            key = np.packbits(mask).tobytes()
            if key not in solved_masks:
                solved_masks[key] = minimum_rectangle_cover(mask)
            cover = solved_masks[key]
            layers.append({"primitive_class": primitive, "occurrence": occurrence,
                           "cells": int(mask.sum()), **cover})
            if not cover["feasible"]:
                return {"feasible": False, "max_useful_n": 0, "layers": layers,
                        "capped": False, "reason": cover.get("reason", "infeasible")}
            total += int(cover["count"])
    return {"feasible": True, "max_useful_n": min(total, cap), "matrix_max_useful_n": total,
            "layers": layers, "capped": total > cap, "optimal": True}
