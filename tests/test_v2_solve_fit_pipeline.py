from types import SimpleNamespace

import rebar_service.v2_pipeline as mod
from rebar_service.config import Settings
from rebar_service.v2_jobs import V2Job
from rebar_service.v2_pipeline import V2Pipeline


class FakeStore:
    def __init__(self):
        self.jobs = []
        self.states = []
        self.artifacts = {
            "prepared_problem": {"work_matrix": [[1]], "prepared": {"dummy": True}},
            "field_context": {
                "field": {"field_geometry": None},
                "cfg": {"recipes": {}, "densities": {1: 1.0}, "diameters": {1: 20.0}, "steps": {1: 150.0}, "axis": "y", "back_grid": (18.0, 300.0)},
                "params": {"min_width_mm": 300.0, "anchor_factor": 40.0},
            },
        }
        self.task = {
            "task_id": "t",
            "config": {"solver": {"solver_time_limit": 12.0}},
            "solutions": [{"n": 4, "attempt": 1, "state": "pending"}],
        }

    def get_v2_task(self, task_id):
        return self.task

    def get_v2_n(self, task_id, n):
        return next((dict(r) for r in self.task["solutions"] if r["n"] == n), None)

    def set_v2_n_state(self, task_id, n, state, **kwargs):
        self.states.append((n, state, kwargs))
        row = next(r for r in self.task["solutions"] if r["n"] == n)
        row["state"] = state
        row.update({k: v for k, v in kwargs.items() if k in {"status", "result", "error"} and v is not None})
        return True

    def load_v2_artifact(self, task_id, key):
        return self.artifacts.get(key)

    def save_v2_artifact(self, task_id, key, artifact_type, value, **kwargs):
        self.artifacts[key] = value

    def enqueue_v2_job(self, job):
        self.jobs.append(job)
        return True


def test_v2_solve_feasible_persists_raw_result_and_enqueues_fit(monkeypatch):
    store = FakeStore()
    settings = Settings(default_solver_threads=2, max_solver_threads=4, solver_backend="highs")
    pipeline = V2Pipeline(store, settings)
    seen = {}

    def fake_solve(problem, n, **kwargs):
        seen.update(n=n, kwargs=kwargs)
        return {"n": n, "is_feasible": True, "is_optimal": False, "total_cost": 123.0, "rectangles": [(0, 0, 0, 0, 1)]}

    monkeypatch.setattr(mod, "solve_v2_problem", fake_solve)
    pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=4, attempt=1))

    assert store.states[0][1] == "solving"
    assert seen["n"] == 4
    assert seen["kwargs"]["threads"] == 2
    assert seen["kwargs"]["solver_time_limit"] == 12.0
    assert store.artifacts["solver:n:4:attempt:1"]["is_feasible"] is True
    assert [(j["stage"], j["kind"], j["n"]) for j in store.jobs] == [("fitting", "fit", 4)]


def test_v2_solve_proven_infeasible_is_terminal_success(monkeypatch):
    store = FakeStore()
    pipeline = V2Pipeline(store, Settings())
    monkeypatch.setattr(mod, "solve_v2_problem", lambda *a, **k: {
        "n": 4, "is_feasible": False, "status": "Infeasible", "solve_state": "infeasible"
    })

    pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=4, attempt=1))

    assert store.jobs == []
    n, state, kwargs = store.states[-1]
    assert (n, state, kwargs["status"]) == (4, "success", "infeasible")


def test_v2_solve_operational_failure_raises_for_worker_to_mark_error(monkeypatch):
    store = FakeStore()
    pipeline = V2Pipeline(store, Settings())
    monkeypatch.setattr(mod, "solve_v2_problem", lambda *a, **k: {
        "n": 4, "is_feasible": False, "error": "worker exited", "solve_state": "failed"
    })

    try:
        pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=4, attempt=1))
    except RuntimeError as exc:
        assert "worker exited" in str(exc)
    else:
        raise AssertionError("operational solver failure must propagate")


def test_v2_fit_feasible_builds_no_anchorage_compact_zones_and_enqueues_baring(monkeypatch):
    store = FakeStore()
    store.task["solutions"][0]["state"] = "solving"
    store.artifacts["solver:n:4:attempt:1"] = {
        "n": 4, "is_feasible": True, "is_optimal": True, "rectangles": [(0, 0, 0, 0, 1)]
    }
    pipeline = V2Pipeline(store, Settings())
    fitted = {
        "n": 4,
        "is_feasible": True,
        "is_optimal": True,
        "fit_result": {"rectangles": [(0.0, 0.0, 300.0, 1000.0, 1)]},
        "anchored_boxes": [{
            "class": 1, "diameter": 20.0, "step": 150.0,
            "fitted_bounds": (0.0, 0.0, 300.0, 1000.0),
            "bounds": (0.0, 0.0, 300.0, 1800.0),
            "hold": 800.0,
        }],
    }
    monkeypatch.setattr(mod, "fit_v2_problem", lambda *a, **k: fitted)

    pipeline.handle_fit(V2Job(stage="fitting", kind="fit", task_id="t", n=4, attempt=1))

    assert store.states[-1][1] == "fitting"
    artifact = store.artifacts["fit:n:4:attempt:1"]
    zone = artifact["zones"][1]  # 0 is background
    assert zone["kind"] == "additional"
    assert zone["length"] == 1000.0
    assert zone["start_anchorage"] == 800.0
    assert zone["end_anchorage"] == 800.0
    # Anchored bounds are deliberately not exposed in compact geometry.
    assert "bounds" not in zone and "anchored_bounds" not in zone
    assert [(j["stage"], j["kind"]) for j in store.jobs] == [("baring", "task_bars")]


def _analytical_store(matrix=((1,),), *, classes=(1,), recipes=None):
    from shapely.geometry import box

    store = FakeStore()
    store.task["solutions"] = [{"n": 1, "attempt": 1, "state": "pending"}]
    store.artifacts["prepared_problem"] = {
        "work_matrix": [list(row) for row in matrix],
        "component": {
            "id": -1,
            "classes": list(classes),
            "demand_bounds": (0.0, 0.0, 300.0, 600.0),
            "bounds": (0.0, 0.0, 300.0, 600.0),
        },
        "prepared": {"dummy": True},
    }
    store.artifacts["field_context"] = {
        "field": {"field_geometry": box(0, 0, 300, 600)},
        "cfg": {
            "recipes": dict(recipes or {}),
            "densities": {1: 1.0, 2: 2.0},
            "diameters": {1: 20.0, 2: 25.0},
            "steps": {1: 150.0, 2: 150.0},
            "axis": "y",
            "back_grid": (18.0, 300.0),
        },
        "params": {"min_width_mm": 300.0, "anchor_factor": 40.0},
    }
    return store


def test_v2_minimum_n_uses_analytical_full_field_cover_without_highs(monkeypatch):
    store = _analytical_store()
    pipeline = V2Pipeline(store, Settings())

    def must_not_run(*args, **kwargs):
        raise AssertionError("HiGHS must not run for analytical minimum N")

    monkeypatch.setattr(mod, "solve_v2_problem", must_not_run)
    pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=1, attempt=1))

    solved = store.artifacts["solver:n:1:attempt:1"]
    assert solved["analytical_min_n_fast_path"] is True
    assert solved["is_feasible"] is True and solved["is_optimal"] is True
    assert solved["analytical_fit"]["anchored_boxes"]
    assert [(j["stage"], j["kind"]) for j in store.jobs] == [("fitting", "fit")]


def test_v2_fit_consumes_analytical_fit_without_refitting(monkeypatch):
    store = _analytical_store()
    pipeline = V2Pipeline(store, Settings())
    monkeypatch.setattr(mod, "solve_v2_problem", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=1, attempt=1))

    monkeypatch.setattr(mod, "fit_v2_problem", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not refit analytical result")))
    pipeline.handle_fit(V2Job(stage="fitting", kind="fit", task_id="t", n=1, attempt=1))

    artifact = store.artifacts["fit:n:1:attempt:1"]
    assert artifact["fit"]["analytical_min_n_fast_path"] is True
    assert artifact["zones"][1]["length"] == 600.0
    assert [(j["stage"], j["kind"]) for j in store.jobs][-1] == ("baring", "task_bars")


def test_v2_n_below_recipe_minimum_is_proven_infeasible_without_highs(monkeypatch):
    store = _analytical_store(classes=(2,), recipes={2: (1, 1)})
    pipeline = V2Pipeline(store, Settings())
    monkeypatch.setattr(mod, "solve_v2_problem", lambda *a, **k: (_ for _ in ()).throw(AssertionError("HiGHS must not run")))

    pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=1, attempt=1))
    _n, state, kwargs = store.states[-1]
    assert state == "success"
    assert kwargs["status"] == "infeasible"
    assert kwargs["result"]["solver_result"]["reason"] == "below_minimum_n"


def test_v2_minimum_n_with_physical_void_falls_back_to_solver(monkeypatch):
    store = _analytical_store(matrix=((1, -1),))
    pipeline = V2Pipeline(store, Settings())
    called = []
    monkeypatch.setattr(mod, "solve_v2_problem", lambda *a, **k: called.append(True) or {
        "n": 1, "is_feasible": True, "is_optimal": False, "total_cost": 1.0,
        "rectangles": [(0, 0, 0, 0, 1)],
    })

    pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=1, attempt=1))
    assert called == [True]
    assert "analytical_min_n_fast_path" not in store.artifacts["solver:n:1:attempt:1"]


def test_v2_fit_artifact_does_not_persist_anchorage_expanded_geometry(monkeypatch):
    from shapely.geometry import box

    store = FakeStore()
    store.task["solutions"][0]["state"] = "solving"
    store.artifacts["solver:n:4:attempt:1"] = {
        "n": 4, "is_feasible": True, "is_optimal": True, "rectangles": [(0, 0, 0, 0, 1)]
    }
    fitted = {
        "n": 4, "is_feasible": True, "is_optimal": True,
        "fit_result": {"rectangles": [(0.0, 0.0, 300.0, 1000.0, 1)]},
        "anchored_boxes": [{
            "class": 1, "diameter": 20.0, "step": 150.0, "hold": 800.0,
            "fitted_bounds": (0.0, 0.0, 300.0, 1000.0),
            "bounds": (0.0, -800.0, 300.0, 1800.0),
            "anchored_bounds_unclipped": (0.0, -800.0, 300.0, 1800.0),
            "geometry": box(0.0, 0.0, 300.0, 1000.0),
        }],
    }
    monkeypatch.setattr(mod, "fit_v2_problem", lambda *a, **k: fitted)
    V2Pipeline(store, Settings()).handle_fit(
        V2Job(stage="fitting", kind="fit", task_id="t", n=4, attempt=1)
    )

    persisted = store.artifacts["fit:n:4:attempt:1"]["fit"]
    layer = persisted["anchored_boxes"][0]
    assert layer["fitted_bounds"] == (0.0, 0.0, 300.0, 1000.0)
    assert layer["hold"] == 800.0
    assert "geometry" not in layer
    assert "bounds" not in layer
    assert "anchored_bounds_unclipped" not in layer


def test_v2_cancel_during_solver_does_not_enqueue_downstream(monkeypatch):
    store = FakeStore()
    pipeline = V2Pipeline(store, Settings())

    def solve_then_cancel(*args, **kwargs):
        store.task["solutions"][0]["state"] = "cancelled"
        return {"n": 4, "is_feasible": True, "is_optimal": True, "total_cost": 1.0,
                "rectangles": [(0, 0, 0, 0, 1)]}

    monkeypatch.setattr(mod, "solve_v2_problem", solve_then_cancel)
    pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=4, attempt=1))
    assert store.jobs == []
    assert store.task["solutions"][0]["state"] == "cancelled"


def test_v2_retry_during_old_solver_does_not_enqueue_stale_attempt(monkeypatch):
    store = FakeStore()
    pipeline = V2Pipeline(store, Settings())

    def solve_then_retry(*args, **kwargs):
        store.task["solutions"][0]["attempt"] = 2
        store.task["solutions"][0]["state"] = "pending"
        return {"n": 4, "is_feasible": True, "is_optimal": True, "total_cost": 1.0,
                "rectangles": [(0, 0, 0, 0, 1)]}

    monkeypatch.setattr(mod, "solve_v2_problem", solve_then_retry)
    pipeline.handle_solve(V2Job(stage="solving", kind="solve", task_id="t", n=4, attempt=1))
    assert store.jobs == []
    assert store.task["solutions"][0]["attempt"] == 2
