from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon, box

from A101.linear_idea import generate_all_rectangles
from A101.reinforcement_components import (
    _mark_matrix_voids,
    filter_candidates_by_matrix_barriers,
    prepare_component_problem,
)


def _rects(matrix):
    a = np.asarray(matrix, dtype=np.int32)
    return generate_all_rectangles(
        int_matrix=a,
        x_steps=np.ones(a.shape[1], dtype=float),
        y_steps=np.ones(a.shape[0], dtype=float),
        xs=np.arange(a.shape[1] + 1, dtype=float),
        min_w=0.0,
        holds={1: 10.0},
    )


def test_solver_candidates_may_cross_background_zero_but_not_void_minus_one():
    over_background = _rects([[1, 0, 1]])
    across_void = _rects([[1, -1, 1]])

    assert (0, 0, 2, 0, 1) in over_background
    assert (0, 0, 2, 0, 1) not in across_void
    assert (0, 0, 0, 0, 1) in across_void
    assert (2, 0, 2, 0, 1) in across_void


def test_pre_mosaic_filter_uses_matrix_minus_one_only():
    matrix = np.array([[1, 0, 1, -1, 1]], dtype=np.int32)
    rectangles = [
        (0, 0, 2, 0, 1),  # crosses only background 0 -> allowed
        (2, 0, 4, 0, 1),  # crosses -1 -> rejected
        (4, 0, 4, 0, 1),
    ]

    kept, rejected = filter_candidates_by_matrix_barriers(rectangles, matrix)

    assert kept == [rectangles[0], rectangles[2]]
    assert rejected == 1


def test_dense_matrix_marks_only_true_physical_void_as_minus_one():
    # Grid has three cells. Left is demand, middle is background material,
    # right has no physical material at all and must become the -1 barrier.
    matrix = np.array([[1, 0, 0]], dtype=np.int32)
    physical = box(0, 0, 1, 1).union(box(1, 0, 2, 1))

    marked = _mark_matrix_voids(
        matrix,
        work_x_edges=np.array([0.0, 1.0, 2.0, 3.0]),
        work_y_edges=np.array([0.0, 1.0]),
        axis="y",
        physical_geometry=physical,
    )

    assert marked.tolist() == [[1, 0, -1]]


"""def test_prepare_problem_does_not_reject_candidate_by_exact_physical_shape_after_matrix_exists():
    # One irregular/diagonal physical cell.  The matrix represents it as one
    # positive cell; after that discretization the exact triangle must not be
    # used to reject the rectangular solver candidate.
    triangle = Polygon([(0, 0), (2, 0), (0, 2)])
    component = {
        "id": 0,
        "polygons": [{"geometry": triangle, "load": 1.0}],
        "classes": [1],
        "loads": [1.0],
        "bounds": triangle.bounds,
        "demand_bounds": triangle.bounds,
    }

    problem = prepare_component_problem(
        component,
        load2cls={0.0: 0, 1.0: 1},
        recipes={},
        densities={0: 0.0, 1: 1.0},
        diameters={1: 10.0},
        anchor_factor=0.0,
        axis="y",
        min_width=0.0,
        grid_size=0.1,
        fill_notches_threshold=None,
        short_edge_threshold=None,
        simplify_steps_threshold=None,
        max_n=10,
        use_mosaic=False,
        preserve_demand_classes=True,
        strict_grid_coverage=True,
        refine_unrepresentable_cells=False,
        refine_mixed_cells=False,
        physical_geometry=triangle,
    )

    assert np.any(problem["work_matrix"] > 0)
    assert "work_physical_mask" not in problem
    for x0, y0, x1, y1, *_ in problem["selectable_rectangles"]:
        assert not np.any(problem["work_matrix"][y0:y1 + 1, x0:x1 + 1] < 0)"""
