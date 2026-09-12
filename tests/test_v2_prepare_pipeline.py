from types import SimpleNamespace

import numpy as np

from rebar_service.v2_jobs import V2Job
from rebar_service.v2_pipeline import V2Pipeline


class FakeStore:
    def __init__(self):
        self.task = {
            "task_id": "t",
            "scene_id": "s",
            "overlay_id": 0,
            "smooth": False,
            "config": {"solver": {"solver_time_limit": None}},
            "solutions": [
                {"n": 2, "attempt": 1, "state": "preparing"},
                {"n": 9, "attempt": 1, "state": "preparing"},
            ],
        }
        self.artifacts = {}
        self.prep_states = []
        self.n_states = []
        self.jobs = []

    def get_v2_task(self, task_id):
        return self.task

    def set_v2_preparation_state(self, task_id, state, **kwargs):
        self.prep_states.append((state, kwargs))
        self.task["preparation_state"] = state
        if kwargs.get("max_useful_n") is not None:
            self.task["max_useful_n"] = kwargs["max_useful_n"]
        if state == "success":
            for row in self.task["solutions"]:
                if row["state"] == "preparing":
                    row["state"] = "pending"

    def save_v2_artifact(self, task_id, key, kind, value, **kwargs):
        self.artifacts[key] = value

    def set_v2_n_state(self, task_id, n, state, **kwargs):
        self.n_states.append((n, state, kwargs))
        for row in self.task["solutions"]:
            if row["n"] == n:
                row["state"] = state
                row.update({k: v for k, v in kwargs.items() if v is not None})
        return True

    def enqueue_v2_job(self, row):
        self.jobs.append(row)
        return True


def test_preparing_computes_max_n_from_matrix_before_full_solver_problem(monkeypatch):
    import rebar_service.v2_pipeline as module

    store = FakeStore()
    order = []
    context = {"cfg": {"recipes": {}}, "component": {"id": -1, "polygons": ["dummy"]}, "field": {"x": 1}}
    matrix_problem = {"work_matrix": np.array([[1, 1]], dtype=np.int32)}
    prepared_problem = {"prepared": {"max_n": 4}}

    monkeypatch.setattr(module, "build_v2_field_context", lambda *args, **kwargs: order.append("field") or context)
    monkeypatch.setattr(module, "build_v2_matrix_problem", lambda *args, **kwargs: order.append("matrix") or matrix_problem)

    def max_n(matrix, **kwargs):
        order.append("max_n")
        assert matrix is matrix_problem["work_matrix"]
        return {"feasible": True, "max_useful_n": 4, "matrix_max_useful_n": 4, "capped": False}

    monkeypatch.setattr(module, "estimate_max_useful_n", max_n)

    def full(matrix_arg, context_arg, settings, *, max_n):
        order.append(("full", max_n))
        assert matrix_arg is matrix_problem
        assert context_arg is context
        return prepared_problem

    monkeypatch.setattr(module, "build_v2_solver_problem", full)

    pipeline = V2Pipeline(store, SimpleNamespace(effective_prepare_max_n=lambda: 100))
    pipeline.dispatch(V2Job(stage="preparing", kind="prepare", task_id="t"))

    assert order == ["field", "matrix", "max_n", ("full", 4)]
    assert store.artifacts["prepared_problem"] is prepared_problem
    assert store.prep_states[-1][0] == "success"
    assert [job["n"] for job in store.jobs] == [2]
    assert store.jobs[0]["stage"] == "solving"
    assert (9, "error") == store.n_states[-1][0:2]
