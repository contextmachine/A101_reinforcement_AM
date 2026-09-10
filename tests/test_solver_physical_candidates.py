from __future__ import annotations

import numpy as np
import pytest
from A101.reinforcement_components import filter_candidates_by_matrix_barriers
from A101.select_min_density_rectangles_recipes import _rectangle_area_with_hold


def test_candidate_crossing_matrix_void_is_rejected_but_background_is_allowed():
    matrix = np.array([[1, 0, -1, 1]], dtype=int)
    rectangles = [
        (0, 0, 1, 0, 1),  # background 0 is traversable
        (0, 0, 3, 0, 1),  # crosses -1 void
        (3, 0, 3, 0, 1),
    ]
    kept, rejected = filter_candidates_by_matrix_barriers(rectangles, matrix)
    assert kept == [rectangles[0], rectangles[2]]
    assert rejected == 1


def test_objective_anchorage_cost_is_symmetric_at_field_edges():
    x = np.array([0.0, 100.0, 200.0, 300.0])
    y = np.array([0.0, 1000.0])
    left = _rectangle_area_with_hold(x, y, 0, 0, 0, 0, 1, {1: 400.0}, "x")
    middle = _rectangle_area_with_hold(x, y, 1, 0, 1, 0, 1, {1: 400.0}, "x")
    right = _rectangle_area_with_hold(x, y, 2, 0, 2, 0, 1, {1: 400.0}, "x")
    assert left[1] == pytest.approx(middle[1]) == pytest.approx(right[1])
    assert left[1] == pytest.approx((100 + 800) * 1000)
