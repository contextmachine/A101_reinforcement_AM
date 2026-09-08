import pytest

from rebar_service.config import Settings
from rebar_service.pipeline import PipelineJob, PipelineWorkflow


class RefreshStore:
    settings = Settings()

    def __init__(self):
        self.jobs = []
        self.meta = {"cancelled": False, "paused": False}
        self.pending = 0
        self.saved = None
        self.filters = None

    def get_meta(self, task_id):
        return self.meta

    def pending_jobs(self, task_id):
        return self.pending

    def generation(self, task_id):
        return 3

    def solution_summaries(self, task_id, **kwargs):
        self.filters = kwargs
        return [{"solution_id": "s1", "total_N": 7, "source": "components", "variant": "smooth", "overlay_id": 42}]

    def enqueue_pipeline_job(self, job):
        if any(x["job_id"] == job["job_id"] for x in self.jobs):
            return False
        self.jobs.append(job)
        return True

    def load_solution(self, task_id, solution_id, **kwargs):
        return {"solution_id": "s1", "candidate_id": "c1", "variant": "smooth", "overlay_id": 42,
                "source": "components", "total_N": 7, "component_ns": {"0": 7},
                "component_choices": {}, "anchored_boxes": [], "bar_layout": {"old": True},
                "mass_metrics": {"with_anchorage_kg": 999}, "validation": {"old": True}}

    def load_candidate(self, *a, **kw):
        raise AssertionError("Refresh must not require a deleted candidate artifact")

    def save_solution(self, task_id, solution):
        self.saved = solution

    def best_solution(self, *a, **kw):
        return None

    def publish_event(self, *a, **kw):
        return "1"


def test_refresh_defaults_to_preview_and_only_queries_summaries():
    from rebar_service.relayout import schedule_layout_refresh
    store = RefreshStore()
    result = schedule_layout_refresh(store, "t", variant="smooth", overlay_id=42, refresh_id="r1")
    assert result["selected"] == 1
    assert result["queued"] == 0
    assert not store.jobs
    assert store.filters == {"variant": "smooth", "overlay_id": 42}


def test_refresh_enqueues_only_layout_for_specific_solution_context():
    from rebar_service.relayout import schedule_layout_refresh
    store = RefreshStore()
    report = schedule_layout_refresh(store, "t", variant="smooth", overlay_id=42, apply=True, refresh_id="r1")
    assert report["queued"] == 1
    job = store.jobs[0]
    assert job["kind"] == "layout_solution"
    assert job["generation"] == 3
    assert job["payload"]["relayout_solution_id"] == "s1"
    assert job["payload"]["overlay_id"] == 42
    assert job["payload"]["variant"] == "smooth"
    assert job["payload"]["layout_refresh"] == "r1"
    assert schedule_layout_refresh(store, "t", variant="smooth", overlay_id=42, apply=True, refresh_id="r1")["queued"] == 0
    assert schedule_layout_refresh(store, "t", variant="smooth", overlay_id=42, apply=True, refresh_id="r2")["queued"] == 1


@pytest.mark.parametrize("condition", ["pending", "cancelled", "paused"])
def test_refresh_refuses_active_or_cancelled_task(condition):
    from rebar_service.relayout import schedule_layout_refresh
    store = RefreshStore()
    if condition == "pending":
        store.pending = 1
    else:
        store.meta[condition] = True
    with pytest.raises(ValueError):
        schedule_layout_refresh(store, "t", variant="smooth", overlay_id=42, apply=True)
    assert not store.jobs


def test_worker_refresh_updates_same_solution_without_candidate(monkeypatch):
    store = RefreshStore()
    workflow = PipelineWorkflow(store, store.settings)
    observed = {}
    def layout(task_id, candidate):
        observed.update(candidate)
        return {**candidate, "solution_id": "new-deterministic-id", "actual_mass_kg": 5,
                "is_feasible": True, "is_optimal": False, "status": "feasible", "mass_metrics": {"schema_version": 2}}
    monkeypatch.setattr(workflow, "_layout_candidate", layout)
    job = PipelineJob("layout_solution", "t", {"relayout_solution_id": "s1", "variant": "smooth", "overlay_id": 42, "layout_refresh": "r1"}, generation=3)
    workflow.dispatch(job)
    assert store.saved["solution_id"] == "s1"
    assert store.saved["mass_metrics"]["schema_version"] == 2
    assert store.saved["metadata"]["layout_refresh_id"] == "r1"
    assert "bar_layout" not in observed
    assert "mass_metrics" not in observed
    assert "validation" not in observed


def test_real_layout_refresh_restores_jsonb_geometry_and_new_metrics(monkeypatch):
    from shapely.geometry import box
    from A101.axis_orientation import add_box_anchorage
    from rebar_service.postgres_store import json_safe_value
    field = box(0, 0, 1800, 2300)
    store = RefreshStore()
    workflow = PipelineWorkflow(store, store.settings)
    cfg = {"axis": "x", "back_grid": (16, 300), "rebar_config_source": "user"}
    monkeypatch.setattr(workflow, "_field", lambda *a: {"cfg": cfg, "start_polygons": [{"geometry": field}]})
    monkeypatch.setattr(workflow, "_params", lambda *a: {"anchor_factor": 40, "steel_density_kg_m3": 7850})
    monkeypatch.setattr(workflow, "_solver", lambda *a: {"threads": 1})
    anchors = add_box_anchorage([(100, 500, 1700, 2100, 1)], recipes={}, diameters={1: 20},
                               steps={1: 150}, anchor_factor=40, axis="x", field=field)
    saved = json_safe_value({"solution_id": "s1", "candidate_id": "c1", "source": "components",
                            "variant": "smooth", "overlay_id": 42, "total_N": 1,
                            "component_ns": {"0": 1}, "component_choices": {},
                            "anchored_boxes": anchors, "mass_metrics": {"with_anchorage_unclipped_kg": 0}})
    monkeypatch.setattr(store, "load_solution", lambda *a, **k: saved)
    workflow.dispatch(PipelineJob("layout_solution", "t", {
        "relayout_solution_id": "s1", "variant": "smooth", "overlay_id": 42, "layout_refresh": "real",
    }, generation=3))
    assert store.saved["solution_id"] == "s1"
    assert store.saved["total_N"] == 1
    assert store.saved["is_feasible"]
    m = store.saved["mass_metrics"]
    assert m["schema_version"] == 2
    assert m["with_anchorage_unclipped_kg"] >= m["with_anchorage_kg"]
    assert m["with_anchorage_kg"] == pytest.approx(store.saved["actual_mass_kg"])
