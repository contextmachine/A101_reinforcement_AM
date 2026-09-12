"""Lattice-tightened candidate pool: contract of generate_all_rectangles, cap, coverage, hook."""

from __future__ import annotations

import numpy as np
import pytest

from A101.lattice_candidates import lattice_candidates
from A101.linear_idea import generate_all_rectangles


def _matrix(seed, ny=14, nx=11, classes=3, void=True):
    rng = np.random.default_rng(seed)
    A = rng.integers(0, classes + 1, size=(ny, nx)).astype(np.int32)
    A[rng.random((ny, nx)) < 0.45] = 0
    if void:
        A[3:5, 4:6] = -1
    return A


def _edges(nx, step=300.0):
    return np.arange(nx + 1, dtype=float) * step


def _check_contract(A, pool, min_w, edges):
    ny, nx = A.shape
    assert pool == sorted(set(pool))
    for x1, y1, x2, y2, cls in pool:
        assert 0 <= x1 <= x2 < nx and 0 <= y1 <= y2 < ny
        block = A[y1:y2 + 1, x1:x2 + 1]
        assert (block >= 0).all(), "candidate spans a void"
        assert cls == block.max() > 0, "class must be the highest class inside"
        assert edges[x2 + 1] - edges[x1] >= min(min_w, edges[-1]) - 1e-9 or (x1 == 0 and x2 == nx - 1)
    covered = np.zeros(A.shape, bool)
    for x1, y1, x2, y2, cls in pool:
        block = A[y1:y2 + 1, x1:x2 + 1]
        covered[y1:y2 + 1, x1:x2 + 1] |= (block > 0) & (block <= cls)
    assert covered[A > 0].all(), "every demand cell needs a covering candidate"


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_pool_respects_the_candidate_contract_and_covers_all_demand(seed):
    A = _matrix(seed)
    edges = _edges(A.shape[1])
    pool, info = lattice_candidates(A, x_edges=edges, min_w=900.0, cap=400)
    _check_contract(A, pool, 900.0, edges)
    assert info["generator"] == "lattice" and info["candidates"] == len(pool)
    assert info["lattice"] >= 1


def test_lattice_one_is_the_finest_pool_and_the_cap_coarsens_it():
    A = _matrix(5, ny=20, nx=16, void=False)
    edges = _edges(A.shape[1])
    fine, info1 = lattice_candidates(A, x_edges=edges, min_w=0.0, cap=10**9)
    coarse, info2 = lattice_candidates(A, x_edges=edges, min_w=0.0, cap=200)
    assert info1["lattice"] == 1 and info2["lattice"] > 1
    assert len(coarse) <= 200 < len(fine)
    # every coarse box is a box of the fine (canonical) pool: tightening makes it lattice-1 aligned
    assert set(coarse) <= set(fine)


def test_forced_lattice_and_tightening_reproduce_the_demand_bounding_boxes():
    A = np.zeros((6, 6), np.int32)
    A[1:3, 1:3] = 2
    A[4, 4] = 1
    edges = _edges(6)
    pool, info = lattice_candidates(A, x_edges=edges, min_w=0.0, cap=10**9, lattice=6)
    # a single 6x6 lattice box tightened to the demand bbox (1,1)-(4,4) with class 2
    assert info["lattice"] == 6
    assert (1, 1, 4, 4, 2) in pool
    # plus the rescue boxes needed so that the class-1 cell is covered by a class>=1 box
    assert any(x1 <= 4 <= x2 and y1 <= 4 <= y2 for x1, y1, x2, y2, _ in pool)


def test_min_width_widens_narrow_boxes_with_background_columns():
    A = np.zeros((4, 8), np.int32)
    A[:, 3] = 1  # one demand column, 300 mm wide
    edges = _edges(8)
    pool, _ = lattice_candidates(A, x_edges=edges, min_w=1000.0, cap=10**9, lattice=1)
    assert pool and all(edges[x2 + 1] - edges[x1] >= 1000.0 - 1e-9 for x1, _, x2, _, _ in pool)
    assert all(x1 <= 3 <= x2 for x1, _, x2, _, _ in pool)


def test_pool_never_spans_a_void():
    A = np.zeros((5, 5), np.int32)
    A[:, :] = 1
    A[2, 2] = -1
    edges = _edges(5)
    pool, _ = lattice_candidates(A, x_edges=edges, min_w=0.0, cap=10**9, lattice=1)
    _check_contract(A, pool, 0.0, edges)
    assert all(not (x1 <= 2 <= x2 and y1 <= 2 <= y2) for x1, y1, x2, y2, _ in pool)


def test_finest_pool_contains_every_exhaustive_candidate_tightened():
    A = _matrix(7, ny=9, nx=7)
    edges = _edges(A.shape[1])
    fine, _ = lattice_candidates(A, x_edges=edges, min_w=0.0, cap=10**9, lattice=1)
    exhaustive = generate_all_rectangles(int_matrix=A, x_steps=np.diff(edges), y_steps=np.ones(A.shape[0]),
                                         xs=edges, min_w=0.0, holds=0)
    fine_set = set(fine)
    for x1, y1, x2, y2, cls in exhaustive:
        block = A[y1:y2 + 1, x1:x2 + 1] > 0
        ys, xs = np.nonzero(block)
        tight = (x1 + xs.min(), y1 + ys.min(), x1 + xs.max(), y1 + ys.max(), cls)
        assert tight in fine_set
