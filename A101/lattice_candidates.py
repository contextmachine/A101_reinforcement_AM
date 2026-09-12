"""Lattice-tightened candidate rectangles for the whole-field MILP.

Alternative to :func:`A101.linear_idea.generate_all_rectangles`.  Instead of enumerating every
pair of demand columns and every run of demand rows, boxes are taken whose edges lie on every
``lattice``-th cell edge of the work matrix and each box is *tightened* to the bounding box of the
demand cells it contains.  Tightening keeps the covered demand-cell set and never raises the
cost, so the pool is canonical for that lattice; ``lattice = 1`` is the full canonical pool.
The lattice is the tractability dial: the pool size scales roughly with ``lattice**-4`` and
:func:`lattice_candidates` picks the finest lattice whose pool fits ``cap``.

Candidates follow the production contract (see ``prepare_component_problem``):

* ``(x1, y1, x2, y2, cls)`` in matrix indices, inclusive; columns ``x`` run across the bars,
  rows ``y`` along them;
* ``cls`` is the highest demand class inside the box;
* the box contains no void cell (``-1``);
* its cross extent is at least ``min_w`` (widened with background columns when needed, as the
  production enumerator does with its ``x2_start`` rule);
* every demand cell of the matrix is inside at least one candidate whose class covers it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np


def lattice_candidates(
    work_matrix: np.ndarray,
    *,
    x_edges: Sequence[float],
    min_w: float,
    cap: int,
    lattice: int = 0,
) -> tuple[list[tuple[int, int, int, int, int]], dict[str, Any]]:
    """Return ``(candidates, info)``; ``lattice > 0`` forces one lattice, else the finest under ``cap``."""
    A = np.asarray(work_matrix)
    if lattice > 0:
        pool = _pool(A, x_edges=x_edges, min_w=min_w, lattice=int(lattice))
        chosen = int(lattice)
    else:
        chosen, pool = _auto(A, x_edges=x_edges, min_w=min_w, cap=int(cap))
    pool = _ensure_cover(A, pool, x_edges=x_edges, min_w=min_w)
    info = {"generator": "lattice", "lattice": chosen, "cap": int(cap), "candidates": len(pool)}
    return pool, info


def _auto(A: np.ndarray, *, x_edges: Sequence[float], min_w: float, cap: int) -> tuple[int, list]:
    ny, nx = A.shape
    largest = max(1, min(ny, nx))
    best_l, best = None, None
    for L in range(1, largest + 1):
        pool = _pool(A, x_edges=x_edges, min_w=min_w, lattice=L)
        if best is None or len(pool) <= cap:
            best_l, best = L, pool
        if len(pool) <= cap:
            return best_l, best
    return best_l, best  # type: ignore[return-value]


def _pool(A: np.ndarray, *, x_edges: Sequence[float], min_w: float, lattice: int) -> list:
    ny, nx = A.shape
    demand = A > 0
    if not demand.any():
        return []
    void = A < 0
    edges = np.asarray(x_edges, dtype=float)
    rows = sorted({*range(0, ny, lattice), ny})
    cols = sorted({*range(0, nx, lattice), nx})
    # column-cumulative over rows (demand per column of a row band) and row-cumulative over cols
    cum_j = np.zeros((ny + 1, nx), np.int64); cum_j[1:] = np.cumsum(demand, 0)
    cum_i = np.zeros((ny, nx + 1), np.int64); cum_i[:, 1:] = np.cumsum(demand, 1)
    void_c = _integral(void)
    classes = sorted(int(c) for c in np.unique(A[demand]))
    ge = {c: _integral(A >= c) for c in classes}
    out = []
    for a in range(len(rows) - 1):
        for b in range(a, len(rows) - 1):
            j0, j1 = rows[a], rows[b + 1] - 1
            colcnt = cum_j[j1 + 1] - cum_j[j0]
            occ = np.flatnonzero(colcnt)
            if len(occ) == 0:
                continue
            nxt = np.searchsorted(occ, cols[:-1], "left")
            prv = np.searchsorted(occ, [c - 1 for c in cols[1:]], "right") - 1
            xs_, ys_ = np.triu_indices(len(cols) - 1, 0)
            lo, hi = nxt[xs_], prv[ys_]
            ok = (lo < len(occ)) & (hi >= 0) & (lo <= hi)
            if not ok.any():
                continue
            i0s, i1s = occ[lo[ok]], occ[hi[ok]]
            sub = cum_i[j0:j1 + 1]
            cnt = sub[:, i1s + 1] - sub[:, i0s]
            hit = cnt > 0
            jj0 = j0 + np.argmax(hit, 0)
            jj1 = j1 - np.argmax(hit[::-1], 0)
            cand = np.unique(np.stack([i0s, jj0, i1s, jj1], 1), axis=0)
            out.append(cand)
    if not out:
        return []
    cand = np.unique(np.concatenate(out), axis=0)
    cand = _widen(cand, edges, min_w, nx)
    cand = cand[_box_sum(void_c, cand) == 0]
    cls = np.zeros(len(cand), np.int64)
    for c in classes:
        cls = np.where(_box_sum(ge[c], cand) > 0, c, cls)
    keep = cls > 0
    cand, cls = cand[keep], cls[keep]
    result = np.unique(np.concatenate([cand, cls[:, None]], 1), axis=0)
    return [tuple(int(v) for v in row) for row in result]


def _integral(mask: np.ndarray) -> np.ndarray:
    c = np.zeros((mask.shape[0] + 1, mask.shape[1] + 1), np.int64)
    c[1:, 1:] = np.cumsum(np.cumsum(mask.astype(np.int64), 0), 1)
    return c


def _box_sum(c: np.ndarray, cand: np.ndarray) -> np.ndarray:
    """Sum of an integral image over inclusive boxes ``(x1, y1, x2, y2)``."""
    x1, y1, x2, y2 = cand[:, 0], cand[:, 1], cand[:, 2], cand[:, 3]
    return c[y2 + 1, x2 + 1] - c[y1, x2 + 1] - c[y2 + 1, x1] + c[y1, x1]


def _widen(cand: np.ndarray, edges: np.ndarray, min_w: float, nx: int) -> np.ndarray:
    """Widen boxes narrower than ``min_w`` with background columns (right first, then left)."""
    if min_w <= 0:
        return cand
    cand = cand.copy()
    for k in range(len(cand)):
        x1, x2 = int(cand[k, 0]), int(cand[k, 2])
        while edges[x2 + 1] - edges[x1] < min_w - 1e-9:
            if x2 + 1 < nx:
                x2 += 1
            elif x1 > 0:
                x1 -= 1
            else:
                break
        cand[k, 0], cand[k, 2] = x1, x2
    return cand


def _ensure_cover(A: np.ndarray, pool: list, *, x_edges: Sequence[float], min_w: float) -> list:
    """Guarantee every demand cell has a covering candidate; fall back to the production enumerator."""
    demand = A > 0
    if not demand.any():
        return pool
    covered = np.zeros(A.shape, bool)
    for x1, y1, x2, y2, cls in pool:
        block = A[y1:y2 + 1, x1:x2 + 1]
        covered[y1:y2 + 1, x1:x2 + 1] |= (block > 0) & (block <= cls)
    missing = demand & ~covered
    if not missing.any():
        return pool
    from .linear_idea import generate_all_rectangles

    ny, nx = A.shape
    x_steps = np.diff(np.asarray(x_edges, dtype=float))
    extra = generate_all_rectangles(
        int_matrix=A, x_steps=x_steps, y_steps=np.ones(ny), xs=np.asarray(x_edges, dtype=float),
        min_w=min_w, holds=0,
    )
    ys, xs = np.nonzero(missing)
    rescue = {
        r for r in extra
        if any((r[0] <= x <= r[2]) and (r[1] <= y <= r[3]) and A[y, x] <= r[4] for y, x in zip(ys, xs))
    }
    return sorted(set(pool) | rescue)
