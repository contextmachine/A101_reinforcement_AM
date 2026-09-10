from __future__ import annotations

import numpy as np

from A101.max_n_milp import estimate_max_useful_n, minimum_rectangle_cover


def test_minimum_rectangle_cover_is_exact_for_small_mask():
    mask = np.array([[1, 1], [1, 1]], dtype=bool)
    rectangles = [(0, 0, 1, 1, 1), (0, 0, 0, 1, 1), (1, 0, 1, 1, 1)]
    assert minimum_rectangle_cover(mask, rectangles)["count"] == 1


def test_layer_recipe_counts_repeated_primitive_layers_from_matrix_only():
    matrix = np.array([[2, 2], [2, 2]], dtype=int)
    result = estimate_max_useful_n(matrix, recipes={2: (1, 1)}, hard_cap=100)
    assert result["feasible"] is True
    assert result["max_useful_n"] == 2
    assert [row["count"] for row in result["layers"]] == [1, 1]


def test_background_zero_is_forbidden_inside_max_n_rectangle():
    matrix = np.array([[1, 0, 1]], dtype=int)
    result = estimate_max_useful_n(matrix, hard_cap=100)
    assert result["max_useful_n"] == 2
    assert result["layers"][0]["cells"] == 2


def test_void_minus_one_is_forbidden_inside_max_n_rectangle():
    matrix = np.array([[1, -1, 1]], dtype=int)
    result = estimate_max_useful_n(matrix, hard_cap=100)
    assert result["max_useful_n"] == 2


def test_recipe_compatible_classes_share_only_the_primitive_occurrence_they_require():
    # class 2 == two occurrences of primitive class 1.  Occurrence #1 may use
    # class 1 and class 2 cells, but 0 splits the rectangle. Occurrence #2 may
    # use only class 2 cells. Expected covers: 2 + 2 = 4 rectangles.
    matrix = np.array([[1, 2, 0, 2]], dtype=int)
    result = estimate_max_useful_n(matrix, recipes={2: (1, 1)}, hard_cap=100)
    assert result["feasible"] is True
    assert result["matrix_max_useful_n"] == 4
    assert [row["count"] for row in result["layers"]] == [2, 2]


def test_legacy_problem_mapping_ignores_physical_mask_and_uses_only_work_matrix():
    # Backward-compatible callers may still pass the historical problem dict,
    # but physical-mask data must have no effect on max-N anymore.
    problem = {
        "work_matrix": np.array([[1, 1]], dtype=int),
        "work_physical_mask": np.array([[True, False]], dtype=bool),
        "selectable_rectangles": [],
    }
    result = estimate_max_useful_n(problem, hard_cap=100)
    assert result["feasible"] is True
    assert result["max_useful_n"] == 1


def test_hard_cap_stops_large_layer_sum():
    matrix = np.array([[2]], dtype=int)
    result = estimate_max_useful_n(matrix, recipes={2: tuple([1] * 120)}, hard_cap=100)
    assert result["max_useful_n"] == 100
    assert result["capped"] is True
