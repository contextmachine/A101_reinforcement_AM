import numpy as np
from shapely.geometry import box

from A101.max_n_milp import estimate_max_useful_n
from A101.reinforcement_components import (
    prepare_component_matrix,
    prepare_component_problem_from_matrix,
)


def _component():
    geometry = box(0, 0, 600, 600)
    return {
        "id": -1,
        "polygons": [{"geometry": geometry, "load": 1.0, "class": 1}],
        "classes": [1],
        "loads": [1.0],
        "bounds": geometry.bounds,
        "demand_bounds": geometry.bounds,
    }


def test_matrix_preparation_is_available_before_candidate_model_and_feeds_max_n():
    component = _component()
    matrix_problem = prepare_component_matrix(
        component,
        load2cls={0.0: 0, 1.0: 1},
        recipes={},
        densities={0: 0.0, 1: 1.0},
        diameters={1: 10.0},
        anchor_factor=0.0,
        axis="y",
        grid_size=300.0,
        fill_notches_threshold=None,
        short_edge_threshold=None,
        simplify_steps_threshold=None,
        preserve_demand_classes=True,
        strict_grid_coverage=True,
        refine_unrepresentable_cells=False,
        refine_mixed_cells=False,
        physical_geometry=box(0, 0, 600, 600),
    )
    assert isinstance(matrix_problem["work_matrix"], np.ndarray)
    assert "prepared" not in matrix_problem
    assert "work_rectangles" not in matrix_problem

    max_n = estimate_max_useful_n(matrix_problem["work_matrix"], recipes={}, hard_cap=100)["max_useful_n"]
    assert max_n and max_n > 0

    problem = prepare_component_problem_from_matrix(
        matrix_problem,
        load2cls={0.0: 0, 1.0: 1},
        recipes={},
        densities={0: 0.0, 1: 1.0},
        diameters={1: 10.0},
        anchor_factor=0.0,
        min_width=0.0,
        max_n=max_n,
        use_mosaic=False,
    )
    assert problem["prepared"]["max_n"] == max_n
    assert problem["work_rectangles"]
