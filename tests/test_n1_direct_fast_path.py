from __future__ import annotations

from shapely.geometry import box

from rebar_service.config import Settings
from rebar_service.pipeline import PipelineJob, PipelineWorkflow


class N1Store:
    def __init__(self):
        self.saved = None
        self.field = {
            "cfg": {
                "recipes": {},
                "densities": {1: 10.0, 3: 30.0},
                "diameters": {1: 18, 3: 25},
                "steps": {1: 300, 3: 100},
                "axis": "y",
            },
            "field_geometry": box(0, 0, 100, 100),
        }
        self.problem = {
            "problem": {
                "component": {
                    "id": 0,
                    "demand_bounds": (10, 20, 50, 80),
                    "bounds": (0, 0, 60, 90),
                    "classes": [1, 3],
                }
            }
        }

    def load_field(self, task_id, variant="raw"):
        return self.field

    def load_problem(self, task_id, component_id, variant="raw"):
        return self.problem

    def get_meta(self, task_id):
        return {
            "parameters": {"anchor_factor": 32.0, "solver": {"threads": 1}},
        }

    def is_n_cancelled(self, task_id, n):
        return False

    def save_frontier_result(self, task_id, component_id, n, value, variant="raw"):
        self.saved = dict(value)


def test_single_component_frontier_does_not_call_fit_box_layout(monkeypatch):
    import A101.fit_box_layout as fit_module

    monkeypatch.setattr(
        fit_module,
        "fit_box_layout",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fit MILP must not run for N=1")),
    )

    workflow = PipelineWorkflow(N1Store(), Settings())
    result = workflow._single_component_frontier("task", 0, variant="raw")

    assert result["n"] == 1
    assert result["rectangles"] == [(10.0, 20.0, 50.0, 80.0, 3)]
    assert result["solver_result"]["n1_fast_path"] is True
    assert result["fit_result"]["n1_fast_path"] is True


def test_explicit_n1_uses_direct_fast_path_even_without_fallback(monkeypatch):
    store = N1Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow._single_component_frontier = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "n": 1, "is_feasible": True, "is_optimal": True, "variant": "raw", "smooth": False
    }
    called = []
    workflow._frontier_ready = lambda *args, **kwargs: called.append(args)  # type: ignore[method-assign]

    workflow.handle_solve_component(
        PipelineJob("solve_component", "task", {"component_id": 0, "n": 1, "variant": "raw", "force_single_box": False})
    )

    assert store.saved["n"] == 1
    assert called
