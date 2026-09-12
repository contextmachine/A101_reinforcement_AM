"""Bottom-up agglomerative zoning: BVH-style merging with a beam-search endgame.

Prototype (2026-08-04). Deliberately independent of the existing optimiser
modules; reuses only the input pipeline (dxf_io / catalog / grid).

Model:
  * atom    = deficit raster cell (100 mm grid in (u, v), u = bar direction)
  * cluster = axis-aligned box; by construction always the bbox of the deficit
    cells merged into it (edge rows/columns of a box contain deficit cells,
    and the invariant survives bbox-union merges)
  * a zone's rebar option = worst requirement anywhere inside its box, bars run
    the box's full u-span plus 40d anchorage each side  ->  cluster cost is a
    function of the box alone, so a merge loss is exact and O(#ladder levels)
  * no spacing/gap constraints: boxes may touch or overlap; cost is as drawn
  * greedy phase: cheapest merge first (priority queue over nearby pairs)
  * endgame:      lockstep beam (width x branch) over merge choices
  * every merge appends one (K, mass) point; every recorded K is a valid
    full-coverage layout; the knee of the curve is "Точка 3"

Run:  poetry run python experiments/agglo.py "examples/*.dxf" -o out_agglo
"""
from __future__ import annotations

import argparse
import glob
import heapq
import itertools
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from a101_reinforcement.catalog import map_bands, resolve_background  # noqa: E402
from a101_reinforcement.dxf_io import read_mosaic  # noqa: E402
from a101_reinforcement.grid import RequirementGrid, build_grid, denoise_bands  # noqa: E402

STEEL = 7850.0
ANCH_D = 40  # anchorage = 40d beyond the required mosaic, each side


# --------------------------------------------------------------------------- #
# Harvest sink: every box the search constructs, for the ILP referee
# (experiments/ilp.py).  Off by default and free when off.
# --------------------------------------------------------------------------- #
_SINK: list | None = None
_SINK_N = 0
_SINK_ALL = False


def harvest_start(all_costed: bool = False) -> None:
    """Collect constructed boxes.  ``all_costed`` also takes every box that was
    merely *priced* (all beam/greedy pair unions), not just those that entered a
    layout."""
    global _SINK, _SINK_N, _SINK_ALL
    _SINK, _SINK_N, _SINK_ALL = [], 0, all_costed


def harvest_stop() -> np.ndarray:
    """Return the deduped (n, 4) pool of harvested boxes and stop collecting."""
    global _SINK, _SINK_N, _SINK_ALL
    buf, _SINK, _SINK_N, _SINK_ALL = _SINK, None, 0, False
    if not buf:
        return np.empty((0, 4), np.int64)
    return np.unique(np.concatenate(buf), axis=0)


def _rec(i0, i1, j0, j1) -> None:
    global _SINK_N
    if _SINK is None:
        return
    a = np.broadcast_arrays(*(np.asarray(x, np.int64) for x in (i0, i1, j0, j1)))
    _SINK.append(np.stack([x.ravel() for x in a], 1))
    _SINK_N += len(_SINK[-1])
    if _SINK_N > 2_000_000:
        _SINK[:] = [np.unique(np.concatenate(_SINK), axis=0)]
        _SINK_N = len(_SINK[0])


# --------------------------------------------------------------------------- #
# Scoring engine: box -> exact zone mass, via per-level integral images
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, grid: RequirementGrid, min_width_fe: int = 2,
                 anch_d: float = ANCH_D):
        self.grid = grid
        self.cell = float(grid.cell_mm)
        self.need = grid.need
        self.L = len(grid.options) - 1  # options[0] is None
        nv, nu = self.need.shape
        self.ge_cum = np.zeros((self.L + 1, nv + 1, nu + 1), np.int64)
        for lvl in range(1, self.L + 1):
            self.ge_cum[lvl, 1:, 1:] = np.cumsum(
                np.cumsum((self.need >= lvl).astype(np.int64), 0), 1
            )
        opts = grid.options
        self.step = np.array([np.inf] + [o.step for o in opts[1:]], float)
        self.diam = np.array([0.0] + [o.diameter for o in opts[1:]], float)
        self.layers = np.array([1] + [o.layers for o in opts[1:]], int)
        self.kg_m1 = np.array(  # linear mass of a SINGLE bar, kg/m
            [0.0] + [STEEL * (np.pi * o.diameter**2 / 4.0) * 1e-6 for o in opts[1:]], float
        )
        self.anch = anch_d * self.diam  # mm, per side (ТЗ: >= 40d beyond the mosaic)
        self.min_w = min_width_fe * float(grid.fe_v_mm)
        self.area = np.array([0.0] + [o.as_cm2_m for o in opts[1:]], float)
        cell_m2 = (self.cell / 1e3) ** 2
        self.ideal_kg = float((self.area[self.need] * 1e-4 * cell_m2).sum() * STEEL)
        self.labels = ["-"] + [
            (f"ф{o.diameter}x{o.layers}" if o.layers > 1 else f"ф{o.diameter}") + f"@{o.step}"
            for o in opts[1:]
        ]

    def rank(self, i0, i1, j0, j1) -> np.ndarray:
        """Highest option index present inside each box (vectorised)."""
        i0, i1, j0, j1 = (np.asarray(x, np.int64) for x in (i0, i1, j0, j1))
        r = np.zeros(i0.shape, np.int32)
        for lvl in range(1, self.L + 1):
            c = self.ge_cum[lvl]
            cnt = c[j1 + 1, i1 + 1] - c[j0, i1 + 1] - c[j1 + 1, i0] + c[j0, i0]
            r = np.where(cnt > 0, lvl, r)
        return r

    def cost(self, i0, i1, j0, j1):
        """Exact zone mass (kg) of each box + its chosen rank (vectorised).

        The chosen rank is the cheapest ladder rung >= the worst need inside
        the box — occasionally a higher rung wins (smaller diameter at denser
        step: shorter anchorage, lighter bars)."""
        i0, i1, j0, j1 = (np.asarray(x, np.int64) for x in (i0, i1, j0, j1))
        if _SINK is not None and _SINK_ALL:
            _rec(i0, i1, j0, j1)
        r_need = self.rank(i0, i1, j0, j1)
        u_mm = (i1 - i0 + 1) * self.cell  # bars: full u-span (box = bbox of deficit)
        w_mm = np.maximum((j1 - j0 + 1) * self.cell, self.min_w)
        best_kg = np.full(r_need.shape, np.inf, float)
        best_r = np.zeros(r_need.shape, np.int32)
        for c in range(1, self.L + 1):
            n_bars = (np.maximum(2, np.rint(w_mm / self.step[c]).astype(np.int64) + 1)
                      * self.layers[c])
            kg_c = n_bars * ((u_mm + 2.0 * self.anch[c]) / 1e3) * self.kg_m1[c]
            upd = (r_need <= c) & (kg_c < best_kg)
            best_kg[upd] = kg_c[upd]
            best_r[upd] = c
        empty = r_need == 0
        best_kg[empty] = 0.0
        best_r[empty] = 0
        return best_kg, best_r

    def zone_rows(self, boxes: np.ndarray) -> list[dict]:
        """Human-readable zone records for a snapshot."""
        b = np.asarray(boxes)
        kg, r = self.cost(b[:, 0], b[:, 1], b[:, 2], b[:, 3])
        g = self.grid
        rows = []
        for (i0, i1, j0, j1), m, rk in zip(b, kg, r):
            u0, u1 = g.u_mm(i0), g.u_mm(i1 + 1)
            v0, v1 = g.v_mm(j0), g.v_mm(j1 + 1)
            x0, y0, x1, y1 = g.to_xy(u0, u1, v0, v1)
            w_eff = max(v1 - v0, self.min_w)
            n_bars = max(2, int(round(w_eff / self.step[rk])) + 1) * int(self.layers[rk])
            rows.append(dict(
                i0=int(i0), i1=int(i1), j0=int(j0), j1=int(j1),
                x0=x0, y0=y0, x1=x1, y1=y1,
                option=self.labels[rk], rank=int(rk), bars=n_bars,
                width_mm=round(w_eff, 1),
                bar_len_mm=round((u1 - u0) + 2 * self.anch[rk], 1),
                kg=round(float(m), 2),
            ))
        return rows

    def ideal_in(self, i0, i1, j0, j1):
        """Per-element minimum mass under each box: every covered cell gets
        exactly its own required intensity (the user's per-zone reference)."""
        i0, i1, j0, j1 = (np.asarray(x, np.int64) for x in (i0, i1, j0, j1))
        acc = np.zeros(np.broadcast(i0, j0).shape, float)
        for lvl in range(1, self.L + 1):
            c = self.ge_cum[lvl]
            cnt = c[j1 + 1, i1 + 1] - c[j0, i1 + 1] - c[j1 + 1, i0] + c[j0, i0]
            acc = acc + cnt * (self.area[lvl] - self.area[lvl - 1])
        return acc * 1e-4 * (self.cell / 1e3) ** 2 * STEEL

    def overlap_kg(self, boxes: np.ndarray) -> float:
        """Redundant steel: sum over cells of (Σ covering intensities - max)."""
        b = np.asarray(boxes, np.int64)
        _, r = self.cost(*(b[:, c] for c in range(4)))
        s = np.zeros(self.need.shape, float)
        m = np.zeros(self.need.shape, float)
        for (i0, i1, j0, j1), rk in zip(b, r):
            v = float(self.area[rk])
            s[j0:j1 + 1, i0:i1 + 1] += v
            sub = m[j0:j1 + 1, i0:i1 + 1]
            np.maximum(sub, v, out=sub)
        return float((s - m).sum() * 1e-4 * (self.cell / 1e3) ** 2 * STEEL)

    def verify_cover(self, boxes: np.ndarray) -> bool:
        """Independent check: every deficit cell inside >=1 box of enough rank."""
        provided = np.zeros_like(self.need)
        b = np.asarray(boxes)
        _, r = self.cost(b[:, 0], b[:, 1], b[:, 2], b[:, 3])
        for (i0, i1, j0, j1), rk in zip(b, r):
            sub = provided[j0:j1 + 1, i0:i1 + 1]
            np.maximum(sub, rk, out=sub)
        mask = self.need > 0
        return bool((provided[mask] >= self.need[mask]).all())


# --------------------------------------------------------------------------- #
# Pair generation helpers
# --------------------------------------------------------------------------- #
def pair_gap_ok(boxes: np.ndarray, a: np.ndarray, b: np.ndarray, delta: int) -> np.ndarray:
    """Boolean mask: box gap (empty cells between) <= delta on both axes."""
    gu = np.maximum(boxes[a, 0], boxes[b, 0]) - np.minimum(boxes[a, 1], boxes[b, 1]) - 1
    gv = np.maximum(boxes[a, 2], boxes[b, 2]) - np.minimum(boxes[a, 3], boxes[b, 3]) - 1
    return (gu <= delta) & (gv <= delta)


def union_boxes(boxes: np.ndarray, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, ...]:
    i0 = np.minimum(boxes[a, 0], boxes[b, 0])
    i1 = np.maximum(boxes[a, 1], boxes[b, 1])
    j0 = np.minimum(boxes[a, 2], boxes[b, 2])
    j1 = np.maximum(boxes[a, 3], boxes[b, 3])
    return i0, i1, j0, j1


# --------------------------------------------------------------------------- #
# Phase 1: greedy agglomeration (priority queue, local neighbourhoods)
# --------------------------------------------------------------------------- #
class Greedy:
    BUCKET = 16  # cells per spatial-hash bucket

    def __init__(self, eng: Engine, delta: int):
        self.eng = eng
        self.delta = delta
        cells = np.argwhere(eng.need > 0)  # (n, 2) rows=(j, i)
        n = len(cells)
        self.boxes = np.empty((n, 4), np.int64)  # i0 i1 j0 j1
        self.boxes[:, 0] = self.boxes[:, 1] = cells[:, 1]
        self.boxes[:, 2] = self.boxes[:, 3] = cells[:, 0]
        kg, _ = eng.cost(*(self.boxes[:, k] for k in range(4)))
        _rec(*(self.boxes[:, k] for k in range(4)))
        self.kg = kg.astype(float)
        self.alive = np.ones(n, bool)
        self.ver = np.zeros(n, np.int32)
        self.total = float(self.kg.sum())
        self.K = n
        self.heap: list = []
        self.tick = itertools.count()
        self.buckets: dict[tuple[int, int], set[int]] = {}
        for cid in range(n):
            self._register(cid)
        self.curve: dict[int, float] = {n: self.total}
        self.snaps: dict[int, np.ndarray] = {}

    # -- spatial hash ------------------------------------------------------- #
    def _bucket_range(self, cid: int):
        i0, i1, j0, j1 = self.boxes[cid]
        d, bs = self.delta, self.BUCKET
        return (
            range((i0 - d) // bs, (i1 + d) // bs + 1),
            range((j0 - d) // bs, (j1 + d) // bs + 1),
        )

    def _register(self, cid: int):
        ri, rj = self._bucket_range(cid)
        for bi in ri:
            for bj in rj:
                self.buckets.setdefault((bi, bj), set()).add(cid)

    def _neighbours(self, cid: int) -> np.ndarray:
        ri, rj = self._bucket_range(cid)
        found: set[int] = set()
        for bi in ri:
            for bj in rj:
                s = self.buckets.get((bi, bj))
                if s:
                    dead = {c for c in s if not self.alive[c]}
                    s -= dead
                    found |= s
        found.discard(cid)
        if not found:
            return np.empty(0, np.int64)
        cand = np.fromiter(found, np.int64)
        ok = pair_gap_ok(self.boxes, np.full(len(cand), cid), cand, self.delta)
        return cand[ok]

    # -- queue -------------------------------------------------------------- #
    def _push_pairs(self, a_ids: np.ndarray, b_ids: np.ndarray):
        if len(a_ids) == 0:
            return
        i0, i1, j0, j1 = union_boxes(self.boxes, a_ids, b_ids)
        kg, _ = self.eng.cost(i0, i1, j0, j1)
        delta = kg - self.kg[a_ids] - self.kg[b_ids]
        for d, a, b in zip(delta, a_ids, b_ids):
            heapq.heappush(
                self.heap,
                (float(d), next(self.tick), int(a), int(b),
                 int(self.ver[a]), int(self.ver[b])),
            )

    def _seed_pairs(self):
        """Initial candidate pairs: all alive pairs with gap <= delta (vectorised
        via offset scans over the atom id grid)."""
        need = self.eng.need
        idg = np.full(need.shape, -1, np.int64)
        cells = np.argwhere(need > 0)
        idg[cells[:, 0], cells[:, 1]] = np.arange(len(cells))
        rad = self.delta + 1  # two 1-cell boxes with gap g are (g+1) apart
        offs = [(dj, di) for dj in range(0, rad + 1) for di in range(-rad, rad + 1)
                if (dj, di) > (0, 0)]
        a_all, b_all = [], []
        nv, nu = need.shape
        for dj, di in offs:
            js = slice(0, nv - dj); jd = slice(dj, nv)
            if di >= 0:
                is_ = slice(0, nu - di); id_ = slice(di, nu)
            else:
                is_ = slice(-di, nu); id_ = slice(0, nu + di)
            src, dst = idg[js, is_], idg[jd, id_]
            m = (src >= 0) & (dst >= 0)
            a_all.append(src[m]); b_all.append(dst[m])
        a = np.concatenate(a_all); b = np.concatenate(b_all)
        self._push_pairs(a, b)

    def _bridge(self, delta_far: int) -> bool:
        """When the queue dries up: connect remaining clusters within delta_far."""
        ids = np.flatnonzero(self.alive)
        if len(ids) < 2 or len(ids) > 4000:
            return False
        a, b = np.triu_indices(len(ids), 1)
        a, b = ids[a], ids[b]
        ok = pair_gap_ok(self.boxes, a, b, delta_far)
        if not ok.any():
            return False
        self._push_pairs(a[ok], b[ok])
        return True

    # -- main loop ---------------------------------------------------------- #
    def run(self, stop_k: int, delta_far: int, snap_max: int, want: set[int]):
        self._seed_pairs()
        while self.K > stop_k:
            if not self.heap:
                if not self._bridge(delta_far):
                    break
                continue
            d, _, a, b, va, vb = heapq.heappop(self.heap)
            if not (self.alive[a] and self.alive[b]):
                continue
            if self.ver[a] != va or self.ver[b] != vb:
                continue
            # merge b into a
            self.boxes[a, 0] = min(self.boxes[a, 0], self.boxes[b, 0])
            self.boxes[a, 1] = max(self.boxes[a, 1], self.boxes[b, 1])
            self.boxes[a, 2] = min(self.boxes[a, 2], self.boxes[b, 2])
            self.boxes[a, 3] = max(self.boxes[a, 3], self.boxes[b, 3])
            kg, _ = self.eng.cost(*(self.boxes[a, k] for k in range(4)))
            _rec(*(self.boxes[a, k] for k in range(4)))
            self.total += float(kg[()]) - self.kg[a] - self.kg[b]
            self.kg[a] = float(kg[()])
            self.alive[b] = False
            self.ver[a] += 1
            self.K -= 1
            self._register(a)
            nb = self._neighbours(a)
            # absorb: boxes fully inside the merged one are redundant — the
            # container's option covers everything under it by construction
            A = self.boxes[a]
            for c in nb:
                C = self.boxes[c]
                if C[0] >= A[0] and C[1] <= A[1] and C[2] >= A[2] and C[3] <= A[3]:
                    self.total -= self.kg[c]
                    self.alive[c] = False
                    self.K -= 1
            nb = nb[self.alive[nb]]
            self._push_pairs(np.full(len(nb), a), nb)
            self.curve[self.K] = min(self.curve.get(self.K, np.inf), self.total)
            if self.K <= snap_max or self.K in want:
                self.snaps[self.K] = self.boxes[self.alive].copy()
        return self.boxes[self.alive].copy(), self.kg[self.alive].copy()


# --------------------------------------------------------------------------- #
# Phase 2: repair-aware beam search over merge+split decisions
# --------------------------------------------------------------------------- #
def _lookahead(eng: Engine, bx: np.ndarray, ck: np.ndarray, delta_far: int,
               horizon: int) -> float:
    """Value-of-future: sum of the `horizon` cheapest merge deltas from here.

    Optimistic (chosen pairs may share boxes), but it ranks branches by what
    they can still become instead of what they cost right now — the myopia fix
    that plain mass ranking lacks."""
    n = len(bx)
    if n <= 1 or horizon <= 0:
        return 0.0
    a, b = np.triu_indices(n, 1)
    ok = pair_gap_ok(bx, a, b, delta_far)
    a, b = a[ok], b[ok]
    if len(a) == 0:
        return 0.0
    i0, i1, j0, j1 = union_boxes(bx, a, b)
    mkg, _ = eng.cost(i0, i1, j0, j1)
    d = mkg - ck[a] - ck[b]
    used = np.zeros(n, bool)
    total, taken = 0.0, 0
    for idx in np.argsort(d):  # disjoint greedy matching: each box counted once
        pa, pb = int(a[idx]), int(b[idx])
        if used[pa] or used[pb]:
            continue
        total += float(d[idx])
        used[pa] = used[pb] = True
        taken += 1
        if taken >= horizon:
            break
    return total


def _local_exchange(eng: Engine, bx: np.ndarray, ck: np.ndarray, delta_far: int,
                    focus: list[int], rounds: int = 2):
    """K-preserving exchange restricted to `focus` boxes (split one of them in
    two + apply the cheapest disjoint merge, when the pair nets negative)."""
    bx = np.asarray(bx, np.int64)
    ck = np.asarray(ck, float)
    for _ in range(rounds):
        n = len(bx)
        if n < 3:
            break
        a_idx, b_idx = np.triu_indices(n, 1)
        ok = pair_gap_ok(bx, a_idx, b_idx, delta_far)
        a_idx, b_idx = a_idx[ok], b_idx[ok]
        if len(a_idx) == 0:
            break
        i0, i1, j0, j1 = union_boxes(bx, a_idx, b_idx)
        mkg, _ = eng.cost(i0, i1, j0, j1)
        dm = mkg - ck[a_idx] - ck[b_idx]
        order = np.argsort(dm)[:40]
        applied = False
        for f in focus:
            if not (0 <= f < n):
                continue
            parts, gain = best_cut(eng, bx[f], float(ck[f]), require_two=True)
            if parts is None:
                continue
            for mi in order:
                pa, pb = int(a_idx[mi]), int(b_idx[mi])
                if f in (pa, pb):
                    continue
                if float(dm[mi]) - gain >= -1e-6:
                    break  # merges sorted ascending: no better disjoint combo
                keep = [r for r in range(n) if r not in (f, pa, pb)]
                new = ([tuple(bx[r]) for r in keep]
                       + [(int(i0[mi]), int(i1[mi]), int(j0[mi]), int(j1[mi]))]
                       + [tuple(p) for p in parts])
                bx = np.array(new, np.int64)
                _rec(*(bx[:, c] for c in range(4)))
                nkg, _ = eng.cost(*(bx[:, c] for c in range(4)))
                ck = nkg.astype(float)
                focus = [len(bx) - 3, len(bx) - 2, len(bx) - 1]
                applied = True
                break
            if applied:
                break
        if not applied:
            break
    return bx, ck


def beam_phase(eng: Engine, boxes: np.ndarray, kg: np.ndarray, *, width: int,
               branch: int, delta_far: int, curve: dict, snaps: dict,
               horizon: int = 8, exchange_from: int = 0):
    tick = itertools.count()
    states = [(float(np.asarray(kg, float).sum()), next(tick),
               np.asarray(boxes, np.int64), np.asarray(kg, float), -1)]
    while True:
        children, seen = [], set()
        for total, _, bx, ck, _last in states:
            n = len(bx)
            if n <= 1:
                continue
            a, b = np.triu_indices(n, 1)
            ok = pair_gap_ok(bx, a, b, delta_far)
            a, b = a[ok], b[ok]
            if len(a) == 0:
                continue
            i0, i1, j0, j1 = union_boxes(bx, a, b)
            mkg, _ = eng.cost(i0, i1, j0, j1)
            delta = mkg - ck[a] - ck[b]
            top = np.argsort(delta)[:branch]
            for t in top:
                ai, bi = int(a[t]), int(b[t])
                u = (int(i0[t]), int(i1[t]), int(j0[t]), int(j1[t]))
                _rec(*u)
                # absorb: every box fully inside the union is redundant —
                # its rank is a max over a subset of the union's cells
                inside = ((bx[:, 0] >= u[0]) & (bx[:, 1] <= u[1])
                          & (bx[:, 2] >= u[2]) & (bx[:, 3] <= u[3]))
                inside[ai] = inside[bi] = True
                keep = ~inside
                nbx = np.vstack([bx[keep], np.array(u, np.int64)[None, :]])
                nck = np.append(ck[keep], float(mkg[t]))
                key = nbx[np.lexsort(nbx.T[::-1])].tobytes()
                if key in seen:
                    continue
                seen.add(key)
                children.append((float(nck.sum()), nbx, nck, len(nbx) - 1))
        if not children:
            break
        scored = []
        for total, nbx, nck, last in children:
            val = total + _lookahead(eng, nbx, nck, delta_far, horizon)
            scored.append((val, total, next(tick), nbx, nck, last))
        scored.sort(key=lambda s: (s[0], s[1], s[2]))
        by_k: dict[int, list] = {}
        for s in scored:
            by_k.setdefault(len(s[3]), []).append(s)
        states = []
        for k, group in by_k.items():  # absorption spreads children over Ks
            kept = group[:width]
            greedy_best = min(group, key=lambda s: (s[1], s[2]))
            if greedy_best not in kept:  # elitism: plain-greedy child survives
                kept = kept[:-1] + [greedy_best]
            for _val, total, _, nbx, nck, last in kept:
                if exchange_from and k <= exchange_from:
                    nbx, nck = _local_exchange(eng, nbx, nck, delta_far,
                                               [last, int(np.argmax(nck))])
                    total = float(nck.sum())
                states.append((total, next(tick), nbx, nck, last))
                if total < curve.get(k, np.inf):
                    curve[k] = total
                    snaps[k] = nbx.copy()


# --------------------------------------------------------------------------- #
# Repair: split light boxes around heavier overlapping ones (flank surgery)
# --------------------------------------------------------------------------- #
def tighten(eng: Engine, i0: int, i1: int, j0: int, j1: int):
    """Shrink a box to the bbox of deficit cells inside it (None if empty)."""
    c = eng.ge_cum[1]
    cols = c[j1 + 1, i0 + 1:i1 + 2] - c[j0, i0 + 1:i1 + 2] \
        - c[j1 + 1, i0:i1 + 1] + c[j0, i0:i1 + 1]
    nz = np.flatnonzero(cols)
    if len(nz) == 0:
        return None
    ni0, ni1 = i0 + int(nz[0]), i0 + int(nz[-1])
    rows = c[j0 + 1:j1 + 2, ni1 + 1] - c[j0 + 1:j1 + 2, ni0] \
        - c[j0:j1 + 1, ni1 + 1] + c[j0:j1 + 1, ni0]
    nz = np.flatnonzero(rows)
    nj0, nj1 = j0 + int(nz[0]), j0 + int(nz[-1])
    _rec(ni0, ni1, nj0, nj1)
    return (ni0, ni1, nj0, nj1)


def best_cut(eng: Engine, box, base_kg: float, require_two: bool = False):
    """Best single guillotine cut of a box into tightened parts -> (parts, gain).

    With require_two, only cuts yielding exactly two parts count, keeping the
    zone-count arithmetic of exchange moves exact."""
    i0, i1, j0, j1 = (int(x) for x in box)
    best_parts, best_gain = None, -np.inf
    for axis in (0, 1):
        lo, hi = (i0, i1) if axis == 0 else (j0, j1)
        for cpos in range(lo, hi):
            if axis == 0:
                halves = [(i0, cpos, j0, j1), (cpos + 1, i1, j0, j1)]
            else:
                halves = [(i0, i1, j0, cpos), (i0, i1, cpos + 1, j1)]
            parts = [t for h in halves if (t := tighten(eng, *h))]
            if not parts or (require_two and len(parts) != 2):
                continue
            arr = np.array(parts, np.int64)
            pkg, _ = eng.cost(*(arr[:, c] for c in range(4)))
            gain = base_kg - float(pkg.sum())
            if gain > best_gain:
                best_parts, best_gain = parts, gain
    return best_parts, best_gain


def split_repair(eng: Engine, boxes: np.ndarray):
    """Improve a layout by splitting boxes while total mass drops.

    Two moves, applied to a fixpoint:
      * flank split — a lighter box overlapping a heavier one is replaced by
        up to 4 tightened flanks around the overlap.  Coverage survives with
        no member tracking: every deficit cell of the old box is either under
        the heavier box (which provides >= its need) or inside a flank.
      * guillotine split — the best single cut of a box into two tightened
        halves ("участки разной длины" of the ТЗ): pays one extra pair of
        anchorages to stop paying max bar length across the whole width.
    Every accepted move lowers mass and raises K by <= 3; the beam then
    re-descends the K axis from the repaired state.
    """
    bx = [tuple(b) for b in boxes]
    kg, rk = eng.cost(*(boxes[:, c] for c in range(4)))
    kg, rk = list(kg.astype(float)), list(rk)

    def replace(a: int, parts: list[tuple[int, int, int, int]]):
        del bx[a], kg[a], rk[a]
        if not parts:  # box was fully redundant (entirely under a heavier one)
            return
        arr = np.array(parts, np.int64)
        pkg, prk = eng.cost(*(arr[:, c] for c in range(4)))
        bx.extend(map(tuple, parts))
        kg.extend(map(float, np.atleast_1d(pkg)))
        rk.extend(map(int, np.atleast_1d(prk)))

    def delete_pass() -> bool:
        """Drop boxes whose every deficit cell is covered by the UNION of the
        other boxes at sufficient rank — redundancy invisible to pairwise
        rules (a box can be useless without lying inside any single box)."""
        n = len(bx)
        if n < 2:
            return False
        for z in range(n):
            Z = bx[z]
            sub_need = eng.need[Z[2]:Z[3] + 1, Z[0]:Z[1] + 1]
            mask = sub_need > 0
            if not mask.any():
                replace(z, [])
                return True
            provided = np.zeros(sub_need.shape, np.int32)
            for o in range(n):
                if o == z:
                    continue
                O = bx[o]
                oi0, oi1 = max(Z[0], O[0]), min(Z[1], O[1])
                oj0, oj1 = max(Z[2], O[2]), min(Z[3], O[3])
                if oi0 > oi1 or oj0 > oj1:
                    continue
                sl = provided[oj0 - Z[2]:oj1 - Z[2] + 1, oi0 - Z[0]:oi1 - Z[0] + 1]
                np.maximum(sl, int(rk[o]), out=sl)
            if (provided[mask] >= sub_need[mask]).all():
                replace(z, [])
                return True
        return False

    def flank_pass() -> bool:
        for a in range(len(bx)):
            for b in range(len(bx)):
                # split a around b whenever b is at least as strong — equal
                # ranks included, else duplicated same-option boxes survive
                if a == b or rk[a] > rk[b]:
                    continue
                A, B = bx[a], bx[b]
                oi0, oi1 = max(A[0], B[0]), min(A[1], B[1])
                oj0, oj1 = max(A[2], B[2]), min(A[3], B[3])
                if oi0 > oi1 or oj0 > oj1:
                    continue
                cand = []
                if A[0] <= oi0 - 1:
                    cand.append((A[0], oi0 - 1, A[2], A[3]))
                if oi1 + 1 <= A[1]:
                    cand.append((oi1 + 1, A[1], A[2], A[3]))
                if A[2] <= oj0 - 1:
                    cand.append((oi0, oi1, A[2], oj0 - 1))
                if oj1 + 1 <= A[3]:
                    cand.append((oi0, oi1, oj1 + 1, A[3]))
                flanks = [t for f in cand if (t := tighten(eng, *f))]
                if flanks:
                    arr = np.array(flanks, np.int64)
                    fkg, _ = eng.cost(*(arr[:, c] for c in range(4)))
                    new_mass = float(fkg.sum())
                else:
                    new_mass = 0.0
                if new_mass < kg[a] - 1e-6:
                    replace(a, flanks)
                    return True
        return False

    def cut_pass() -> bool:
        # worst-first: attack the zone with the biggest gap to its
        # per-element minimum — that's where a split+tighten pays most
        arr = np.array(bx, np.int64)
        waste = np.array(kg) - eng.ideal_in(*(arr[:, c] for c in range(4)))
        for a in np.argsort(-waste):
            parts, gain = best_cut(eng, bx[a], kg[a])
            if parts is not None and gain > 1e-6:
                replace(int(a), parts)
                return True
        return False

    for _ in range(400):
        if delete_pass():
            continue
        if flank_pass():
            continue
        if not cut_pass():
            break
    return np.array(bx, np.int64), np.array(kg, float)


def drop_union_redundant(eng: Engine, boxes: np.ndarray) -> np.ndarray:
    """Delete every zone whose deficit cells are all covered, at sufficient
    rank, by the union of the remaining zones (heaviest victim first)."""
    bx = np.asarray(boxes, np.int64).copy()
    while len(bx) >= 2:
        kg, rk = eng.cost(*(bx[:, c] for c in range(4)))
        victim = -1
        for z in range(len(bx)):
            Z = bx[z]
            sub = eng.need[Z[2]:Z[3] + 1, Z[0]:Z[1] + 1]
            mask = sub > 0
            if mask.any():
                prov = np.zeros(sub.shape, np.int32)
                for o in range(len(bx)):
                    if o == z:
                        continue
                    O = bx[o]
                    oi0, oi1 = max(Z[0], O[0]), min(Z[1], O[1])
                    oj0, oj1 = max(Z[2], O[2]), min(Z[3], O[3])
                    if oi0 > oi1 or oj0 > oj1:
                        continue
                    sl = prov[oj0 - Z[2]:oj1 - Z[2] + 1, oi0 - Z[0]:oi1 - Z[0] + 1]
                    np.maximum(sl, int(rk[o]), out=sl)
                if not (prov[mask] >= sub[mask]).all():
                    continue
            if victim < 0 or kg[z] > kg[victim]:
                victim = z
        if victim < 0:
            break
        bx = np.delete(bx, victim, 0)
    return bx


def exchange_sweep(eng: Engine, boxes: np.ndarray, delta_far: int, top_m: int = 60):
    """K-preserving exchange: split one box in two + merge a disjoint pair.

    Applies the best jointly-improving (net mass < 0) combination — the move
    class the repair↔descend alternation cannot reach, since it only ever takes
    individually improving steps.  Returns (boxes, kg, moved)."""
    bx = np.asarray(boxes, np.int64)
    n = len(bx)
    kg, _ = eng.cost(*(bx[:, c] for c in range(4)))
    kg = kg.astype(float)
    if n < 3:
        return bx, kg, False
    a_idx, b_idx = np.triu_indices(n, 1)
    ok = pair_gap_ok(bx, a_idx, b_idx, delta_far)
    a_idx, b_idx = a_idx[ok], b_idx[ok]
    if len(a_idx) == 0:
        return bx, kg, False
    i0, i1, j0, j1 = union_boxes(bx, a_idx, b_idx)
    mkg, _ = eng.cost(i0, i1, j0, j1)
    dm = mkg - kg[a_idx] - kg[b_idx]
    morder = np.argsort(dm)[:top_m]
    best = None  # (net, merge_row, split_box, parts)
    for a in range(n):
        parts, gain = best_cut(eng, bx[a], float(kg[a]), require_two=True)
        if parts is None:
            continue
        for mi in morder:  # ascending by merge cost: first disjoint is optimal
            pa, pb = int(a_idx[mi]), int(b_idx[mi])
            if a in (pa, pb):
                continue
            net = float(dm[mi]) - gain
            if net < -1e-6 and (best is None or net < best[0]):
                best = (net, int(mi), a, parts)
            break
    if best is None:
        return bx, kg, False
    _, mi, a, parts = best
    pa, pb = int(a_idx[mi]), int(b_idx[mi])
    keep = [r for r in range(n) if r not in (a, pa, pb)]
    new = ([tuple(bx[r]) for r in keep]
           + [(int(i0[mi]), int(i1[mi]), int(j0[mi]), int(j1[mi]))]
           + [tuple(p) for p in parts])
    arr = np.array(new, np.int64)
    _rec(*(arr[:, c] for c in range(4)))
    nkg, _ = eng.cost(*(arr[:, c] for c in range(4)))
    return arr, nkg.astype(float), True


def polish(eng: Engine, curve: dict, snaps: dict, want: set, args, deadline: float):
    """Iterate split-repair -> beam re-descent -> exchange at the anchor Ks
    (curve argmin, current knee, every requested K) until nothing improves or
    the time budget runs out."""
    eps = 1e-6
    while time.perf_counter() < deadline:
        mass_min = min(curve.values())
        knee = min(k for k, m in curve.items() if m <= mass_min * (1 + args.knee_tol))
        base = {min(curve, key=curve.get), knee, *want}
        spread = {k + d for k in base for d in (-9, -4, 4, 9) if k + d >= 2}
        anchors = sorted(base | spread, reverse=True)
        improved = False
        round_sum0 = sum(curve.get(k, 0.0) for k in anchors) + mass_min
        for k in anchors:
            if k not in snaps or time.perf_counter() >= deadline:
                continue
            rb, rkg = split_repair(eng, snaps[k])
            rk, rm = len(rb), float(rkg.sum())
            if rm < curve.get(rk, np.inf) - eps:
                curve[rk] = rm
                snaps[rk] = rb.copy()
            beam_phase(eng, rb, rkg, width=args.beam_width, branch=args.branch,
                       delta_far=args.delta_far, curve=curve, snaps=snaps,
                       horizon=args.horizon, exchange_from=args.exchange_from)
            for _ in range(10):  # chain exchanges while they keep paying
                if k not in snaps:
                    break
                ebx, ekg, moved = exchange_sweep(eng, snaps[k], args.delta_far)
                if not moved or float(ekg.sum()) >= curve.get(k, np.inf) - eps:
                    break
                curve[k] = float(ekg.sum())
                snaps[k] = np.asarray(ebx)
                improved = True
        round_sum1 = (sum(curve.get(k, 0.0) for k in anchors)
                      + min(curve.values()))
        if round_sum1 < round_sum0 - eps:
            improved = True
        if not improved:
            break


# --------------------------------------------------------------------------- #
# Per-file driver
# --------------------------------------------------------------------------- #
def solve_file(path: Path, args) -> dict:
    t0 = time.perf_counter()
    mosaic = read_mosaic(path)
    scale = sorted(mosaic.scale, key=lambda b: b[1])
    background = resolve_background(scale[0][2])
    mapping, warns = map_bands(mosaic.scale, background)
    for w in warns:
        print(f"    [scale] {w}")
    bands = denoise_bands(mosaic, args.isolated_fe)
    grid = build_grid(mosaic, bands, mapping, cell_mm=args.cell)
    eng = Engine(grid, min_width_fe=args.min_width_fe,
                 anch_d=getattr(args, "anch_d", ANCH_D))
    n_atoms = int((grid.need > 0).sum())
    if n_atoms == 0:
        print("    no additional reinforcement required")
        return {}

    want = set(args.zones)
    greedy = Greedy(eng, args.delta)
    stop_k = min(args.beam_from, greedy.K)
    t1 = time.perf_counter()
    boxes, kg = greedy.run(stop_k, args.delta_far, args.snap_max, want)
    t2 = time.perf_counter()
    curve, snaps = greedy.curve, greedy.snaps
    beam_phase(eng, boxes, kg, width=args.beam_width, branch=args.branch,
               delta_far=args.delta_far, curve=curve, snaps=snaps,
               horizon=args.horizon, exchange_from=args.exchange_from)
    if args.horizon > 0:
        # portfolio descent: a second, plain-greedy-valued beam into the same
        # curve/snaps — the recorder keeps the better state at every K, so the
        # result dominates either value function alone
        beam_phase(eng, boxes, kg, width=args.beam_width, branch=args.branch,
                   delta_far=args.delta_far, curve=curve, snaps=snaps,
                   horizon=0, exchange_from=args.exchange_from)
    if not args.no_repair:
        polish(eng, curve, snaps, want, args, time.perf_counter() + args.polish_time)
    t3 = time.perf_counter()

    mass_min = min(curve.values())
    knee = min(k for k, m in curve.items() if m <= mass_min * (1 + args.knee_tol))
    picks = sorted({knee, *want})
    if not args.no_repair:
        # monotone refill: more zones must never cost more mass (you can always
        # split) — but repair deletions drop K and the descent can't climb back,
        # so a stale heavier state can linger at the requested K.  Regenerate it
        # from the lightest lower-K state by cheapest splits.
        for k in picks:
            lower = [kk for kk in snaps if kk <= k]
            if not lower:
                continue
            k0 = min(lower, key=lambda kk: curve.get(kk, np.inf))
            if curve.get(k0, np.inf) >= curve.get(k, np.inf) - 1e-6:
                continue
            bx = snaps[k0].copy()
            total = curve[k0]
            for kk in range(k0 + 1, k + 1):
                kg2, _ = eng.cost(*(bx[:, c] for c in range(4)))
                best = None
                for a2 in range(len(bx)):
                    parts, gain = best_cut(eng, bx[a2], float(kg2[a2]),
                                           require_two=True)
                    if parts is not None and (best is None or -gain < best[0]):
                        best = (-gain, a2, parts)
                if best is None:
                    break
                d, a2, parts = best
                bx = np.vstack([np.delete(bx, a2, 0), np.array(parts, np.int64)])
                total += d
                if total < curve.get(kk, np.inf) - 1e-6:
                    curve[kk] = total
                    snaps[kk] = bx.copy()
        mass_min = min(curve.values())
        knee = min(k for k, m in curve.items() if m <= mass_min * (1 + args.knee_tol))
        picks = sorted({knee, *want})
    report = {}
    for k in picks:
        if k not in snaps:
            k_avail = min((kk for kk in snaps if kk >= k), default=None)
            if k_avail is None:
                continue
            k = k_avail
        bx = snaps[k]
        if not args.no_repair:
            # finalize: no reported layout may contain union-redundant zones —
            # delete them all, refill to the requested K by cheapest splits,
            # iterate to a clean fixpoint
            for _ in range(3):
                cleaned = drop_union_redundant(eng, bx)
                if len(cleaned) == len(bx) and len(bx) >= min(k, len(bx)):
                    bx = cleaned
                    break
                bx = cleaned
                while len(bx) < k:
                    kg2, _ = eng.cost(*(bx[:, c] for c in range(4)))
                    best = None
                    for a2 in range(len(bx)):
                        parts, gain = best_cut(eng, bx[a2], float(kg2[a2]),
                                               require_two=True)
                        if parts is not None and (best is None or -gain < best[0]):
                            best = (-gain, a2, parts)
                    if best is None:
                        break
                    bx = np.vstack([np.delete(bx, best[1], 0),
                                    np.array(best[2], np.int64)])
            m = float(eng.cost(*(bx[:, c] for c in range(4)))[0].sum())
            kk = len(bx)
            if m < curve.get(kk, np.inf) - 1e-6:
                curve[kk] = m
                snaps[kk] = bx.copy()
        ok = eng.verify_cover(bx)
        report[k] = dict(mass=float(eng.cost(*(bx[:, c] for c in range(4)))[0].sum()),
                         boxes=bx, cover=ok, overlap=eng.overlap_kg(bx))
    dt = dict(read=t1 - t0, greedy=t2 - t1, beam=t3 - t2)
    return dict(grid=grid, eng=eng, curve=curve, knee=knee, report=report,
                n_atoms=n_atoms, times=dt, name=path.stem)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def dump(res: dict, out: Path, args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    eng: Engine = res["eng"]
    name = res["name"]
    out.mkdir(parents=True, exist_ok=True)
    ks = sorted(res["curve"])
    with open(out / f"{name}.curve.csv", "w", encoding="utf-8") as f:
        f.write("zones,mass_kg\n")
        for k in ks:
            f.write(f"{k},{res['curve'][k]:.1f}\n")

    fig, ax = plt.subplots(figsize=(7, 4.2))
    kk = [k for k in ks if k <= args.curve_xmax]
    ax.plot(kk, [res["curve"][k] for k in kk], lw=1.2)
    ax.axhline(eng.ideal_kg, ls=":", c="green", label=f"ideal (no anchorage) {eng.ideal_kg:.0f} kg")
    knee = res["knee"]
    ax.plot([knee], [res["curve"][knee]], "ro", ms=6,
            label=f"knee: {knee} zones, {res['curve'][knee]:.0f} kg")
    ax.set_xlabel("zones"); ax.set_ylabel("mass, kg")
    ax.set_title(name); ax.legend(); ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(out / f"{name}.curve.png", dpi=130); plt.close(fig)

    need = eng.need
    for k, rep in res["report"].items():
        rows = eng.zone_rows(rep["boxes"])
        with open(out / f"{name}.zones_K{k}.csv", "w", encoding="utf-8") as f:
            cols = list(rows[0].keys())
            f.write(",".join(cols) + "\n")
            for r in rows:
                f.write(",".join(str(r[c]) for c in cols) + "\n")
        fig, ax = plt.subplots(figsize=(12, 12 * need.shape[0] / max(1, need.shape[1])))
        ax.imshow(np.where(need > 0, need, np.nan), origin="lower", cmap="YlOrRd",
                  interpolation="nearest", alpha=0.9)
        cmap = plt.get_cmap("tab10")
        for r in rows:
            i0, i1, j0, j1 = r["i0"], r["i1"], r["j0"], r["j1"]
            c = cmap(r["rank"] % 10)
            ax.add_patch(plt.Rectangle((i0 - 0.5, j0 - 0.5), i1 - i0 + 1, j1 - j0 + 1,
                                       fill=False, ec=c, lw=1.4))
            ax.annotate(r["option"], ((i0 + i1) / 2, (j0 + j1) / 2), color=c,
                        fontsize=6, ha="center", va="center")
        ax.set_title(f"{name} — K={k}, {res['curve'].get(k, 0):.0f} kg, "
                     f"cover={'OK' if rep['cover'] else 'FAIL'}")
        fig.tight_layout(); fig.savefig(out / f"{name}.layout_K{k}.png", dpi=150)
        plt.close(fig)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("patterns", nargs="+")
    ap.add_argument("-o", "--out", default="out_agglo")
    ap.add_argument("--zones", type=lambda s: [int(x) for x in s.split(",") if x],
                    default=[], help="comma-separated zone counts to snapshot")
    ap.add_argument("--cell", type=float, default=100.0)
    ap.add_argument("--min-width-fe", type=int, default=2)
    ap.add_argument("--isolated-fe", type=int, default=1)
    ap.add_argument("--anch-d", type=float, default=ANCH_D,
                    help="anchorage in bar diameters per side (ТЗ says >=40d)")
    ap.add_argument("--delta", type=int, default=5, help="greedy merge reach, cells")
    ap.add_argument("--delta-far", type=int, default=20, help="bridge/beam reach, cells")
    ap.add_argument("--beam-from", type=int, default=150, help="K at which beam takes over")
    ap.add_argument("--beam-width", type=int, default=8)
    ap.add_argument("--branch", type=int, default=4)
    ap.add_argument("--knee-tol", type=float, default=0.02)
    ap.add_argument("--snap-max", type=int, default=300)
    ap.add_argument("--no-repair", action="store_true",
                    help="disable the split-repair/exchange polish")
    ap.add_argument("--polish-time", type=float, default=20.0,
                    help="polish time budget per file, seconds")
    ap.add_argument("--horizon", type=int, default=8,
                    help="beam lookahead: cheapest future merges counted in the value")
    ap.add_argument("--exchange-from", type=int, default=0,
                    help="K below which surviving beam states get local exchanges "
                         "(0=off; measured to hurt — exchanges collapse beam diversity)")
    ap.add_argument("--curve-xmax", type=int, default=300)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    files = sorted({Path(p) for pat in args.patterns for p in glob.glob(pat)})
    files = [f for f in files if f.suffix.lower() == ".dxf"]
    if not files:
        sys.exit("no dxf files matched")
    out = Path(args.out)
    print(f"{'file':<42} {'atoms':>6} {'knee':>5} {'kg@knee':>8} {'ideal':>7} "
          f"{'k':>5} {'t,s':>6}")
    for path in files:
        res = solve_file(path, args)
        if not res:
            continue
        dump(res, out, args)
        knee, curve, eng = res["knee"], res["curve"], res["eng"]
        t = sum(res["times"].values())
        ratio = curve[knee] / eng.ideal_kg if eng.ideal_kg else float("nan")
        cover = all(r["cover"] for r in res["report"].values())
        print(f"{res['name']:<42} {res['n_atoms']:>6} {knee:>5} {curve[knee]:>8.0f} "
              f"{eng.ideal_kg:>7.0f} {ratio:>5.2f} {t:>6.1f}"
              + ("" if cover else "  COVER FAIL"))
        for k in sorted(res["report"]):
            r = res["report"][k]
            tag = " (knee)" if k == knee else ""
            print(f"    K={k:<4d} -> {r['mass']:>8.0f} kg, overlap {r['overlap']:>5.0f} kg{tag}"
                  + ("" if r["cover"] else "  COVER FAIL"))


if __name__ == "__main__":
    main()
