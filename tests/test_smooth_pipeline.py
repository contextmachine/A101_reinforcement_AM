from __future__ import annotations

from rebar_service.config import Settings
from rebar_service.pipeline import PipelineJob, PipelineWorkflow


class QueueStore:
    def __init__(self):
        self.jobs = []
        self.meta = {
            "manual_mode": True,
            "requested_n": [1, 2],
            "scan_mode": "requested",
            "whole": False,
        }

    def get_meta(self, task_id):
        return dict(self.meta)

    def patch_meta(self, task_id, **changes):
        self.meta.update(changes)
        return dict(self.meta)

    def publish_event(self, *args, **kwargs):
        return "1-0"

    def pending_jobs(self, task_id):
        return 0

    def generation(self, task_id):
        return 0

    def enqueue_pipeline_job(self, job):
        self.jobs.append(dict(job))
        return True


def test_pipeline_job_dedupe_distinguishes_raw_and_smooth():
    raw = PipelineJob("solve_component", "t", {"component_id": 1, "n": 2, "variant": "raw"})
    smooth = PipelineJob("solve_component", "t", {"component_id": 1, "n": 2, "variant": "smooth"})
    assert raw.dedupe_key != smooth.dedupe_key


def test_prepare_task_enqueues_requested_variant():
    store = QueueStore()
    workflow = PipelineWorkflow(store, Settings())

    workflow.prepare_task("task", smooth=True)

    assert store.jobs[0]["payload"]["variant"] == "smooth"
    assert store.jobs[0]["payload"]["smooth"] is True


def test_prepare_task_raw_is_default():
    store = QueueStore()
    workflow = PipelineWorkflow(store, Settings())

    workflow.prepare_task("task")

    assert store.jobs[0]["payload"].get("variant", "raw") == "raw"
    assert store.jobs[0]["payload"].get("smooth", False) is False


class PrepareFieldStore:
    def __init__(self):
        self.meta = {
            "parameters": {"axis": "y", "anchor_factor": 32.0},
            "requested_n": [1],
            "scan_mode": "requested",
            "whole": False,
        }
        self.input = {
            "kind": "polygons",
            "units": "mm",
            "polygons": [
                {"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "load": 5.0},
                {"points": [[10, 0], [20, 0], [20, 10], [10, 10]], "load": 2.0},
            ],
        }
        self.fields = {}
        self.jobs = []

    def get_meta(self, task_id):
        return dict(self.meta)

    def patch_meta(self, task_id, **changes):
        self.meta.update(changes)
        return dict(self.meta)

    def get_object(self, task_id, name):
        assert name == "input"
        return self.input

    def publish_event(self, *args, **kwargs):
        return "1-0"

    def pending_jobs(self, task_id):
        return 0

    def generation(self, task_id):
        return 0

    def enqueue_pipeline_job(self, job):
        self.jobs.append(dict(job))
        return True

    def save_field(self, task_id, value, *, variant="raw"):
        self.fields[variant] = value

    def save_component(self, *args, **kwargs):
        raise AssertionError("test decomposition intentionally has no components")


def test_prepare_field_applies_smooth_load_and_stores_smooth_variant(monkeypatch):
    import A101.axis_orientation as axis_module
    import A101.calculate_mass as mass_module
    import A101.grid_work as grid_module
    import A101.poly_bbox as bbox_module
    import A101.read_dxf as dxf_module
    import A101.reinforcement_components as components_module

    smooth_calls = []

    def fake_smooth(polygons):
        smooth_calls.append([p["load"] for p in polygons])
        out = []
        for p in polygons:
            row = dict(p)
            row["old_load"] = row["load"]
            row["load"] = 1.0
            out.append(row)
        return out

    monkeypatch.setattr(dxf_module, "smooth_load", fake_smooth)
    monkeypatch.setattr(axis_module, "class_holds", lambda *a, **k: ({}, {}, {}))
    monkeypatch.setattr(bbox_module, "rect_polygons", lambda rows: rows)
    monkeypatch.setattr(grid_module, "clean_poly", lambda rows: rows)
    monkeypatch.setattr(
        mass_module,
        "resolve_rebar_config",
        lambda *a, **k: {
            "back_grid": (18, 300),
            "stock": [(20, 150)],
            "max_layers": 1,
            "rebar_config_source": "test",
            "load2cls": {1.0: 1},
            "recipes": {},
            "densities": {1: 1.0},
            "diameters": {1: 20},
            "steps": {1: 150},
        },
    )
    monkeypatch.setattr(
        components_module,
        "split_reinforcement_components",
        lambda *a, **k: {
            "components": [],
            "active_indices": [],
            "background_only_indices": [],
            "degenerate_indices": [],
        },
    )

    store = PrepareFieldStore()
    workflow = PipelineWorkflow(store, Settings())
    workflow.handle_prepare_field(
        PipelineJob("prepare_field", "task", {"auto_solve": False, "variant": "smooth", "smooth": True})
    )

    assert smooth_calls == [[5.0, 2.0]]
    assert "smooth" in store.fields
    assert [p["load"] for p in store.fields["smooth"]["start_polygons"]] == [1.0, 1.0]
    assert store.fields["smooth"]["smooth"] is True
    assert store.meta["effective_rebar_config"]["smooth"]["source"] == "test"
