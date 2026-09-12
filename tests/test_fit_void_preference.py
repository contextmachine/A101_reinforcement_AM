import numpy as np
from shapely.geometry import box

from A101.reinforcement_components import fit_component_frontier


def test_fit_component_frontier_prefers_zero_void_overlap_over_lighter_expansion():
    component_problem = {
        "component_id": 0,
        "component": {"id": 0},
        "work_matrix": np.asarray([[1, 1, -1, -1, 1, 1, 0, 0, 0, 1]], dtype=np.int32),
        "work_x_edges": np.arange(0.0, 11.0, 1.0),
        "work_y_edges": np.asarray([0.0, 1.0]),
        "poly_mos": [
            {"geometry": box(0, 0, 2, 1), "class": 1},
            {"geometry": box(4, 0, 6, 1), "class": 1},
            {"geometry": box(9, 0, 10, 1), "class": 1},
        ],
    }
    solver_results = {
        2: {
            "is_feasible": True,
            "is_optimal": True,
            "rectangles": [
                (0, 0, 1, 0, 1),
                (9, 0, 9, 0, 1),
            ],
        }
    }

    result = fit_component_frontier(
        component_problem,
        solver_results,
        recipes={},
        densities={1: 1.0},
        diameters={1: 1.0},
        steps={1: 1.0},
        anchor_factor=0.0,
        axis="y",
        field=box(0, 0, 10, 1),
        min_width=0.0,
        fit_milp_backend="scipy",
        fit_threads=1,
    )[2]

    assert result["is_feasible"] is True
    fitted = [tuple(row[:4]) for row in result["fit_result"]["rectangles"]]
    assert (0.0, 0.0, 2.0, 1.0) in fitted
    assert (4.0, 0.0, 10.0, 1.0) in fitted
    assert (0.0, 0.0, 6.0, 1.0) not in fitted


def test_fit_box_shortlist_prefers_candidate_that_avoids_void():
    from A101.fit_box_layout import fit_box_layout

    polygons = [
        {"geometry": box(0, 0, 2, 1), "class": 1},
        {"geometry": box(4, 0, 6, 1), "class": 1},
        {"geometry": box(9, 0, 10, 1), "class": 1},
    ]
    rectangles = [(0, 0, 2, 1, 1), (9, 0, 10, 1, 1)]

    result = fit_box_layout(
        polygons,
        rectangles,
        densities={1: 1.0},
        allowed_classes={1},
        nearest=1,
        per_direction=0,
        avoid_rectangles=[(2, 0, 4, 1)],
        milp_backend="scipy",
    )

    assert result["is_feasible"] is True
    fitted = [tuple(row[:4]) for row in result["rectangles"]]
    assert (4.0, 0.0, 10.0, 1.0) in fitted
    assert (0.0, 0.0, 6.0, 1.0) not in fitted


def test_already_covered_min_width_expansion_shifts_away_from_void():
    component_problem = {
        "component_id": 0,
        "component": {"id": 0},
        "work_matrix": np.asarray([[0, 0, -1, -1, 1, 1, 0, 0, 0, 0]], dtype=np.int32),
        "work_x_edges": np.arange(0.0, 11.0, 1.0),
        "work_y_edges": np.asarray([0.0, 4.0]),
        "poly_mos": [{"geometry": box(4, 0, 6, 4), "class": 1}],
    }
    solver_results = {
        1: {
            "is_feasible": True,
            "is_optimal": True,
            "rectangles": [(4, 0, 5, 0, 1)],
        }
    }

    result = fit_component_frontier(
        component_problem,
        solver_results,
        recipes={},
        densities={1: 1.0},
        diameters={1: 1.0},
        steps={1: 1.0},
        anchor_factor=0.0,
        axis="y",
        field=box(0, 0, 10, 4),
        min_width=4.0,
        fit_milp_backend="scipy",
        fit_threads=1,
    )[1]

    assert result["is_feasible"] is True
    fitted = [tuple(row[:4]) for row in result["fit_result"]["rectangles"]]
    assert fitted == [(4.0, 0.0, 8.0, 4.0)]
    assert result["fit_result"]["stats"]["void_overlap_area"] == 0.0
