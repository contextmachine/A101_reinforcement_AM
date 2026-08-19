from rebar_service.models import TaskCreate


def test_task_create_scalar_n():
    task = TaskCreate.model_validate(
        {
            "n": 30,
            "input": {
                "kind": "polygons",
                "polygons": [{"points": [[0, 0], [1, 0], [1, 1]], "load": 1}],
            },
        }
    )
    assert task.n == 30
    assert task.axis == "y"

def test_finalize_solution_marks_grid_optimality_scope(monkeypatch):
    import rebar_service.pipeline as pipeline

    monkeypatch.setattr(pipeline, "world_rectangles", lambda *_: [(0, 0, 1, 1, 1)])
    monkeypatch.setattr(
        pipeline,
        "fit_rebar_layout",
        lambda **_: {"is_feasible": True, "is_optimal": False, "zones": [], "rectangles": []},
    )
    monkeypatch.setattr(pipeline, "rebar_summary", lambda **_: {"N": 1, "mass": 1.0, "zones": []})
    context = {
        "poly_mos": [], "recipes": {}, "steps": {1: 1}, "densities": {1: 1},
        "min_width_mm": 1, "axis": "y", "max_snap_mm": 0, "min_bar_gap_mm": 0,
        "diameters": {1: 1}, "steel_density_kg_m3": 7850, "base_holds": {1: 0},
    }
    result = pipeline.finalize_solution(
        {"is_feasible": True, "is_optimal": True, "rectangles": [(0, 0, 0, 0, 1)], "total_cost": 1.0},
        context,
    )
    assert result["solver_is_optimal"] is True
    assert result["postprocess_is_optimal"] is False
    assert result["optimality_scope"] == "prepared_grid"
