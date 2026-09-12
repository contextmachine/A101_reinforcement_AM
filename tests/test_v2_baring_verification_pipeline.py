from math import hypot, pi

import pytest
from shapely.geometry import box

import rebar_service.v2_pipeline as mod
from rebar_service.config import Settings
from rebar_service.v2_jobs import V2Job
from rebar_service.v2_pipeline import V2Pipeline


class FakeStore:
    def __init__(self):
        self.states = []
        self.request_states = []
        self.artifacts = {
            "field_context": {
                "field": {"start_polygons": [{"geometry": "geom"}]},
                "cfg": {"axis": "y"},
                "params": {},
            },
            "fit:n:4:attempt:1": {
                "n": 4,
                "fit": {"is_optimal": True},
                "zones": [{"id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}}],
            },
            "solver:n:4:attempt:1": {"n": 4, "is_optimal": True, "total_cost": 99.0},
        }
        self.task = {
            "task_id": "t",
            "config": {"axis": "y", "steel_density_kg_m3": 7850.0},
            "solutions": [{"n": 4, "attempt": 1, "state": "fitting"}],
        }
        self.requests = {
            ("bars", "b"): {
                "bars_id": "b", "scene_id": "scene", "overlay_id": 0, "smooth": False,
                "state": "pending", "config": {"axis": "y"},
                "zones": [{"id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}}],
            },
            ("verification", "v"): {
                "verification_id": "v", "scene_id": "scene", "overlay_id": 0, "smooth": False,
                "state": "pending", "config": {"axis": "y", "steel_density_kg_m3": 7850.0, "t": 600.0},
                "zones": [{"id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}}],
            },
        }

    def get_v2_n(self, task_id, n):
        return dict(self.task["solutions"][0])

    def get_v2_task(self, task_id):
        return self.task

    def set_v2_n_state(self, task_id, n, state, **kwargs):
        self.states.append((n, state, kwargs))
        row = self.task["solutions"][0]
        row["state"] = state
        row.update({k: v for k, v in kwargs.items() if k in {"status", "result", "error"} and v is not None})
        return True

    def load_v2_artifact(self, task_id, key):
        return self.artifacts.get(key)

    def save_v2_artifact(self, task_id, key, artifact_type, value, **kwargs):
        self.artifacts[key] = value

    def get_v2_request(self, kind, request_id):
        return self.requests.get((kind, request_id))

    def set_v2_request_state(self, kind, request_id, state, **kwargs):
        self.request_states.append((kind, request_id, state, kwargs))

    def resolved_scene_polygons(self, scene_id, *, variant, overlay_id):
        return [
            {"source_index": 0, "load": 5.0, "overlay_state": "active", "points": [[0,0],[1,0],[1,1]]}
        ]


def test_task_baring_completes_n_with_real_mass_and_result(monkeypatch):
    store = FakeStore()
    pipeline = V2Pipeline(store, Settings())
    monkeypatch.setattr(mod, "build_v2_bars_for_context", lambda *a, **k: {
        "bar_layout": {"bars": [{"zone_id": -1, "start": [0,0], "end": [0,1], "d": 18.0}]},
        "mass_metrics": {"additional": {}, "bg": {}},
        "mass_kg": 10.0,
        "mass_bg_kg": 3.0,
    })

    pipeline.handle_task_bars(V2Job(stage="baring", kind="task_bars", task_id="t", n=4, attempt=1))

    assert store.states[0][1] == "baring"
    n, state, kwargs = store.states[-1]
    assert (n, state, kwargs["status"]) == (4, "success", "optimal")
    assert kwargs["fun"] == 99.0
    assert kwargs["mass_kg"] == 10.0
    assert kwargs["mass_bg_kg"] == 3.0
    assert kwargs["result"]["zones"][0]["kind"] == "bg"


def test_standalone_bars_job_persists_success(monkeypatch):
    store = FakeStore()
    pipeline = V2Pipeline(store, Settings(default_steel_density_kg_m3=7850.0))
    monkeypatch.setattr(mod, "build_v2_bars_for_request", lambda *a, **k: {
        "bar_layout": {"bars": []}, "mass_metrics": {"additional": {}, "bg": {}}
    })

    pipeline.handle_bars_request(V2Job(stage="baring", kind="bars_request", task_id="b", request_id="b"))

    assert store.request_states[0][2] == "baring"
    assert store.request_states[-1][2] == "success"
    assert store.request_states[-1][3]["result"]["bar_layout"] == {"bars": []}


def test_verification_job_runs_baring_in_process_and_persists_validation(monkeypatch):
    store = FakeStore()
    pipeline = V2Pipeline(store, Settings())
    calls = []
    monkeypatch.setattr(mod, "build_v2_bars_for_request", lambda *a, **k: calls.append("bars") or {
        "bar_layout": {"bars": [{"zone_id": 1, "start": [0,0], "end": [0,1], "d": 18.0}]},
        "mass_metrics": {"additional": {}, "bg": {}}
    })
    monkeypatch.setattr(mod, "verify_v2_request", lambda *a, **k: calls.append("verify") or [{
        "source_index": 0, "overlay_state": "active", "need_load_sm2/m": 5.0,
        "fact_load_sm2/m": 6.0, "need_load_kg/m3": 1.0, "fact_load_kg/m3": 2.0,
    }])

    pipeline.handle_verification(V2Job(stage="validation", kind="verification", task_id="v", request_id="v"))

    assert calls == ["bars", "verify"]
    assert store.request_states[0][2] == "validation"
    assert store.request_states[-1][2] == "success"
    assert store.request_states[-1][3]["result"][0]["fact_load_sm2/m"] == 6.0


@pytest.mark.parametrize("factor", [0.0, 25.0])
def test_task_baring_persists_background_anchorage_mass_with_real_layout(factor):
    store = FakeStore()
    context = store.artifacts["field_context"]
    context["field"]["start_polygons"] = [{"geometry": box(0, 0, 1000, 1000)}]
    context["params"]["anchor_factor"] = factor
    store.task["config"]["anchor_factor"] = factor

    V2Pipeline(store, Settings()).handle_task_bars(
        V2Job(stage="baring", kind="task_bars", task_id="t", n=4, attempt=1)
    )

    n, state, values = store.states[-1]
    bars = values["result"]["bar_layout"]["bars"]
    expected = sum(
        (hypot(bar["end"][0] - bar["start"][0], bar["end"][1] - bar["start"][1])
         + 2.0 * factor * bar["d"]) * pi * bar["d"]**2 / 4.0 * 1e-9 * 7850.0
        for bar in bars
    )
    assert bars and all(bar["zone_id"] == -1 for bar in bars)
    assert (n, state) == (4, "success")
    assert values["mass_kg"] == pytest.approx(expected)
    assert values["mass_bg_kg"] == pytest.approx(expected)
    assert values["result"]["mass_metrics"]["bg"]["with_anchorage_kg"] == pytest.approx(expected)
    assert store.artifacts["bars:n:4:attempt:1"]["mass_bg_kg"] == pytest.approx(expected)
    # This mass-only correction does not alter the solver objective.
    assert values["fun"] == 99.0


def test_standalone_bars_worker_persists_background_anchorage_metric():
    store = FakeStore()
    store.requests[("bars", "b")]["config"]["anchor_factor"] = 25.0
    store.resolved_scene_polygons = lambda *a, **k: [{
        "source_index": 0, "load": 5.0, "overlay_state": "active",
        "points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
    }]
    V2Pipeline(store, Settings()).handle_bars_request(
        V2Job(stage="baring", kind="bars_request", task_id="b", request_id="b")
    )
    result = store.request_states[-1][3]["result"]
    bars = result["bar_layout"]["bars"]
    expected = sum(
        (hypot(bar["end"][0] - bar["start"][0], bar["end"][1] - bar["start"][1])
         + 2.0 * 25.0 * bar["d"]) * pi * bar["d"]**2 / 4.0 * 1e-9 * 7850.0
        for bar in bars
    )
    assert bars and all(bar["zone_id"] == -1 for bar in bars)
    assert result["mass_metrics"]["bg"]["with_anchorage_kg"] == pytest.approx(expected)
    assert expected > result["mass_metrics"]["bg"]["without_anchorage_kg"]
