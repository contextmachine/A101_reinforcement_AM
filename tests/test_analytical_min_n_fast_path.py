from __future__ import annotations

import numpy as np
from shapely.geometry import box

from rebar_service.config import Settings
from rebar_service.pipeline import PipelineJob, PipelineWorkflow


class AnalyticalStore:
    def __init__(self, *, recipes=None, classes=(3,), matrix=None):
        self.saved_frontier = []
        self.saved_solver = []
        self.field = {
            "cfg": {
                "recipes": dict(recipes or {}),
                "densities": {1: 10.0, 2: 20.0, 3: 30.0, 4: 40.0},
                "diameters": {1: 18, 2: 20, 3: 25, 4: 28},
                "steps": {1: 300, 2: 150, 3: 100, 4: 100},
                "axis": "y",
            },
            "field_geometry": box(0, 0, 100, 100),
        }
        self.problem = {
            "problem": {
                "component": {
                    "id": 0,
                    "demand_bounds": (0, 0, 100, 100),
                    "bounds": (0, 0, 100, 100),
                    "classes": list(classes),
                },
                "work_matrix": np.asarray(matrix if matrix is not None else [[3]], dtype=np.int32),
            }
        }

    def load_field(self, task_id, variant="raw"):
        return self.field

    def load_problem(self, task_id, component_id, variant="raw"):
        return self.problem

    def get_meta(self, task_id):
        return {
            "scene_id": "scene",
            "parameters": {"anchor_factor": 40.0, "solver": {"threads": 1}},
        }

    def is_n_cancelled(self, task_id, n, *args, **kwargs):
        return False

    def save_frontier_result(self, task_id, component_id, n, value, variant="raw", **kwargs):
        self.saved_frontier.append((component_id, int(n), dict(value)))

    def save_solver_result(self, task_id, component_id, n, value, variant="raw", **kwargs):
        self.saved_solver.append((component_id, int(n), value))


def _workflow(store):
    wf = PipelineWorkflow(store, Settings())
    wf._frontier_ready = lambda *args, **kwargs: None
    wf._publish = lambda *args, **kwargs: None
    wf.enqueue = lambda *args, **kwargs: True
    return wf


def test_scene_component_n1_without_recipe_is_analytical_fast_path(monkeypatch):
    import A101.reinforcement_components as rc

    store = AnalyticalStore(recipes={}, classes=(3,), matrix=[[3, 0], [3, 3]])
    wf = _workflow(store)
    monkeypatch.setattr(
        rc,
        "solve_component_frontier",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HiGHS path must not run")),
    )

    wf.handle_solve_component(PipelineJob("solve_component", "task", {"component_id": 0, "n": 1, "variant": "raw"}))

    _, n, result = store.saved_frontier[-1]
    assert n == 1
    assert result["is_feasible"] is True
    assert result["n1_fast_path"] is True
    assert result["rectangles"] == [(0.0, 0.0, 100.0, 100.0, 3)]


def test_recipe_minimum_rejects_smaller_n_and_fast_paths_exact_minimum(monkeypatch):
    import A101.reinforcement_components as rc

    store = AnalyticalStore(recipes={3: (1, 2)}, classes=(3,), matrix=[[3, 0], [3, 3]])
    wf = _workflow(store)
    monkeypatch.setattr(
        rc,
        "solve_component_frontier",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HiGHS path must not run")),
    )

    wf.handle_solve_component(PipelineJob("solve_component", "task", {"component_id": 0, "n": 1, "variant": "raw"}))
    wf.handle_solve_component(PipelineJob("solve_component", "task", {"component_id": 0, "n": 2, "variant": "raw"}))

    low = store.saved_frontier[-2][2]
    exact = store.saved_frontier[-1][2]
    assert low["is_feasible"] is False
    assert low["solve_state"] == "infeasible"
    assert low["reason"] == "below_component_min_n"
    assert low["min_useful_n"] == 2
    assert exact["is_feasible"] is True
    assert exact["analytical_min_n_fast_path"] is True
    assert [row[-1] for row in exact["rectangles"]] == [1, 2]


def test_nested_recipe_minimum_uses_recursive_primitive_layer_count(monkeypatch):
    import A101.reinforcement_components as rc

    store = AnalyticalStore(recipes={4: (2, 3), 3: (1, 2)}, classes=(4,), matrix=[[4]])
    wf = _workflow(store)
    monkeypatch.setattr(
        rc,
        "solve_component_frontier",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HiGHS path must not run")),
    )

    wf.handle_solve_component(PipelineJob("solve_component", "task", {"component_id": 0, "n": 3, "variant": "raw"}))

    result = store.saved_frontier[-1][2]
    assert result["min_useful_n"] == 3
    assert [row[-1] for row in result["rectangles"]] == [2, 1, 2]


def test_matrix_void_disables_analytical_fast_path_and_falls_back_to_solver(monkeypatch):
    import A101.reinforcement_components as rc

    store = AnalyticalStore(recipes={}, classes=(3,), matrix=[[3, -1], [3, 3]])
    wf = _workflow(store)
    calls = []

    def solve(problem, ns, **kwargs):
        calls.append(list(ns))
        return ({1: {"solver_result": {"is_feasible": False}}}, {})

    monkeypatch.setattr(rc, "solve_component_frontier", solve)

    wf.handle_solve_component(PipelineJob("solve_component", "task", {"component_id": 0, "n": 1, "variant": "raw"}))

    assert calls == [[1]]
    assert store.saved_solver
    assert store.saved_frontier == []


def test_scene_whole_n1_without_recipe_uses_same_analytical_fast_path(monkeypatch):
    import A101.reinforcement_components as rc

    store = AnalyticalStore(recipes={}, classes=(3,), matrix=[[3, 0], [3, 3]])
    wf = _workflow(store)
    queued = []
    wf._queue_whole_layout = lambda task_id, result, variant="raw": queued.append(dict(result))  # type: ignore[method-assign]
    monkeypatch.setattr(
        rc,
        "solve_component_frontier",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HiGHS path must not run")),
    )

    wf.handle_solve_whole(PipelineJob("solve_whole", "task", {"component_id": "whole", "n": 1, "variant": "raw"}))

    assert store.saved_frontier[-1][1] == 1
    assert store.saved_frontier[-1][2]["is_feasible"] is True
    assert queued and queued[-1]["n"] == 1
