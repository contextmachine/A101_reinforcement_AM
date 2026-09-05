from rebar_service.models import TaskCreate
from rebar_service.pipeline import to_compat_result


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


def test_current_solution_is_exposed_in_historical_result_shape():
    solution = {
        "solution_id": "s1",
        "source": "components",
        "total_N": 3,
        "component_ns": {"0": 1, "1": 2},
        "proxy_mass": 12.5,
        "actual_mass_kg": 10.25,
        "is_feasible": True,
        "is_optimal": True,
        "rectangles": [(0, 0, 1000, 2000, 1)],
        "anchored_boxes": [(0, 0, 1000, 2000, 1)],
        "component_choices": {
            0: {"solver_result": {"is_feasible": True}, "fit_result": {"is_feasible": True}},
        },
        "bar_layout": {
            "is_feasible": True,
            "zones": [
                {
                    "id": 4,
                    "class": 1,
                    "background": False,
                    "diameter": 20,
                    "step": 150,
                    "primary_bounds": (0, 0, 1000, 2000),
                    "bounds": (0, 0, 1200, 2000),
                    "bars": [(0, 0, 0, 2000)],
                }
            ],
        },
    }
    result = to_compat_result(solution)
    assert result["solution_id"] == "s1"
    assert result["total_N"] == 3
    assert result["component_ns"] == {"0": 1, "1": 2}
    assert result["summary"]["N"] == 3
    assert result["summary"]["mass"] == 10.25
    assert result["summary"]["zones"][0]["final rectangle"] == (0.0, 0.0, 1200.0, 2000.0)
    assert result["fit_result"]["zones"][0]["class"] == 1


def test_settings_accept_unlimited_timeout_markers(monkeypatch):
    from rebar_service.config import Settings

    monkeypatch.setenv("REBAR_SOLVER_TIMEOUT", "none")
    monkeypatch.setenv("REBAR_SOLVER_TIME_LIMIT", "unlimited")
    monkeypatch.setenv("REBAR_FIT_TIME_LIMIT", "null")
    settings = Settings()
    assert settings.solver_timeout is None
    assert settings.solver_time_limit is None
    assert settings.fit_time_limit is None
