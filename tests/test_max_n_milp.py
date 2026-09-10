from __future__ import annotations

import numpy as np

from A101.max_n_milp import estimate_max_useful_n, minimum_rectangle_cover


def test_minimum_rectangle_cover_is_exact_for_small_mask():
    mask = np.array([[1, 1], [1, 1]], dtype=bool)
    rectangles = [(0, 0, 1, 1, 1), (0, 0, 0, 1, 1), (1, 0, 1, 1, 1)]
    assert minimum_rectangle_cover(mask, rectangles)["count"] == 1


def test_layer_recipe_counts_repeated_primitive_layers():
    problem = {
        "work_matrix": np.array([[2, 2], [2, 2]], dtype=int),
        "selectable_rectangles": [(0, 0, 1, 1, 1)],
    }
    result = estimate_max_useful_n(problem, recipes={2: (1, 1)}, hard_cap=100)
    assert result["feasible"] is True
    assert result["max_useful_n"] == 2
    assert [row["count"] for row in result["layers"]] == [1, 1]


def test_separated_mask_needs_two_rectangles():
    problem = {
        "work_matrix": np.array([[1, 0, 1]], dtype=int),
        "selectable_rectangles": [(0, 0, 0, 0, 1), (2, 0, 2, 0, 1)],
    }
    assert estimate_max_useful_n(problem, hard_cap=100)["max_useful_n"] == 2


def test_hard_cap_stops_large_layer_sum():
    matrix = np.array([[2]], dtype=int)
    problem = {"work_matrix": matrix, "selectable_rectangles": [(0, 0, 0, 0, 1)]}
    result = estimate_max_useful_n(problem, recipes={2: tuple([1] * 120)}, hard_cap=100)
    assert result["max_useful_n"] == 100
    assert result["capped"] is True
