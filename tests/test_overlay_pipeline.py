from __future__ import annotations

import pytest

from rebar_service.config import Settings
from rebar_service.pipeline import (
    AnalysisNotPreparedError,
    JobKind,
    PipelineJob,
    PipelineWorkflow,
    overlay_polygon_sets,
)


def test_job_dedupe_identity_includes_overlay_id():
    first = PipelineJob("solve_component", "task", {"component_id": 1, "n": 2, "variant": "raw", "overlay_id": 100})
    second = PipelineJob("solve_component", "task", {"component_id": 1, "n": 2, "variant": "raw", "overlay_id": 101})
    assert first.dedupe_key != second.dedupe_key


def test_overlay_polygon_sets_separate_demand_physical_and_removed():
    rows = [
        {"source_index": 0, "overlay_state": "active", "points": [[0, 0]], "load": 1},
        {"source_index": 1, "overlay_state": "background_only", "points": [[1, 0]], "load": 2},
        {"source_index": 2, "overlay_state": "removed", "points": [[2, 0]], "load": 3},
    ]
    result = overlay_polygon_sets(rows)
    assert [row["source_index"] for row in result["active"]] == [0]
    assert [row["source_index"] for row in result["physical"]] == [0, 1]
    assert result["active_indices"] == [0]
    assert result["background_only_indices"] == [1]
    assert result["removed_indices"] == [2]


class LazyStore:
    def __init__(self):
        self.jobs = []
        self.preparing = False
        self.analysis = {"preparation_state": "stored"}

    def get_meta(self, task_id):
        return {"manual_mode": True, "scan_mode": "requested", "whole": False, "requested_n": [2]}

    def requested_ns(self, task_id, *, variant="raw", overlay_id=0):
        return [2]

    def pending_jobs(self, task_id):
        return 0

    def generation(self, task_id):
        return 0

    def ensure_analysis(self, task_id, *, variant="raw", overlay_id=0):
        return dict(self.analysis)

    def analysis_state(self, task_id, *, variant="raw", overlay_id=0):
        return dict(self.analysis)

    def mark_analysis_preparing(self, task_id, *, variant="raw", overlay_id=0):
        if self.preparing:
            return False
        self.preparing = True
        self.analysis["preparation_state"] = "preparing"
        return True

    def patch_meta(self, task_id, **changes):
        return {**self.get_meta(task_id), **changes}

    def publish_event(self, task_id, event_type, payload, *, overlay_id=0):
        return "1"

    def enqueue_pipeline_job(self, job):
        self.jobs.append(dict(job))
        return True

    def component_ids(self, task_id, *, variant="raw", overlay_id=0):
        return []

    def load_component(self, task_id, component_id, *, variant="raw", overlay_id=0):
        return None


def test_first_task_level_solve_lazily_queues_one_overlay_prepare():
    store = LazyStore()
    workflow = PipelineWorkflow(store, Settings())

    first = workflow.schedule_requested_for_all("task", [2], smooth=True, overlay_id=555)
    second = workflow.schedule_requested_for_all("task", [2], smooth=True, overlay_id=555)

    assert first == {"preparing": [2]}
    assert second == {"preparing": [2]}
    assert len(store.jobs) == 1
    assert store.jobs[0]["kind"] == JobKind.prepare_field.value
    assert store.jobs[0]["payload"]["variant"] == "smooth"
    assert store.jobs[0]["payload"]["overlay_id"] == 555


def test_component_solve_rejects_unprepared_overlay_analysis():
    store = LazyStore()
    workflow = PipelineWorkflow(store, Settings())
    with pytest.raises(AnalysisNotPreparedError):
        workflow.schedule_component_n("task", 0, [1], overlay_id=555)

class OverlayPrepareStore(LazyStore):
    def __init__(self):
        super().__init__()
        self.analysis = {"preparation_state": "preparing"}
        self.meta = {
            "parameters": {"axis": "y", "anchor_factor": 32.0},
            "requested_n": [1], "scan_mode": "requested", "whole": False,
        }
        self.saved_field = None
        self.prepared = False

    def get_meta(self, task_id):
        return dict(self.meta)

    def patch_meta(self, task_id, **changes):
        self.meta.update(changes)
        return dict(self.meta)

    def get_object(self, task_id, name):
        return {"kind": "polygons", "units": "mm", "polygons": self.load_variant_polygons(task_id)}

    def load_variant_polygons(self, task_id, *, variant="raw"):
        return [
            {"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "load": 5.0},
            {"points": [[10, 0], [20, 0], [20, 10], [10, 10]], "load": 4.0},
            {"points": [[20, 0], [30, 0], [30, 10], [20, 10]], "load": 3.0},
        ]

    def resolved_source_polygons(self, task_id, *, variant="raw", overlay_id=0):
        rows = self.load_variant_polygons(task_id, variant=variant)
        states = ["active", "background_only", "removed"]
        return [
            {**row, "source_index": i, "overlay_state": states[i], "active": i == 0, "real": i == 1}
            for i, row in enumerate(rows)
        ]

    def save_field(self, task_id, value, *, variant="raw", overlay_id=0):
        self.saved_field = dict(value)

    def mark_analysis_prepared(self, task_id, *, variant="raw", overlay_id=0):
        self.prepared = True
        self.analysis["preparation_state"] = "prepared"


def test_prepare_field_uses_active_polygons_for_demand_and_real_cleaned_for_physical_field(monkeypatch):
    import A101.axis_orientation as axis_module
    import A101.calculate_mass as mass_module
    import A101.grid_work as grid_module
    import A101.poly_bbox as bbox_module
    import A101.reinforcement_components as components_module

    seen = {}
    monkeypatch.setattr(axis_module, "class_holds", lambda *a, **k: ({}, {}, {}))
    monkeypatch.setattr(grid_module, "clean_poly", lambda rows: rows)
    monkeypatch.setattr(bbox_module, "rect_polygons", lambda rows: seen.setdefault("demand", list(rows)))
    monkeypatch.setattr(
        mass_module,
        "resolve_rebar_config",
        lambda rows, **k: (
            seen.setdefault("config_count", len(rows))
            or {
                "back_grid": (18, 300), "stock": [(20, 150)], "max_layers": 1,
                "rebar_config_source": "test", "load2cls": {5.0: 1}, "recipes": {},
                "densities": {1: 1.0}, "diameters": {1: 20}, "steps": {1: 150},
            }
        ),
    )
    # The expression above returns 3 on first call; use an explicit function for clarity.
    def config(rows, **kwargs):
        seen["config_count"] = len(rows)
        return {
            "back_grid": (18, 300), "stock": [(20, 150)], "max_layers": 1,
            "rebar_config_source": "test", "load2cls": {5.0: 1}, "recipes": {},
            "densities": {1: 1.0}, "diameters": {1: 20}, "steps": {1: 150},
        }
    monkeypatch.setattr(mass_module, "resolve_rebar_config", config)
    monkeypatch.setattr(
        components_module, "split_reinforcement_components",
        lambda *a, **k: {"components": [], "active_indices": [], "background_only_indices": [], "degenerate_indices": []},
    )

    store = OverlayPrepareStore()
    workflow = PipelineWorkflow(store, Settings())
    workflow.dispatch(PipelineJob("prepare_field", "task", {"variant": "raw", "overlay_id": 77, "auto_solve": True}))

    assert seen["config_count"] == 3
    assert len(seen["demand"]) == 1
    assert store.saved_field is not None
    assert [p["source_index"] for p in store.saved_field["start_polygons"]] == [0, 1]
    assert store.saved_field["decomposition"]["active_indices"] == [0]
    assert store.saved_field["decomposition"]["background_only_indices"] == [1]
    assert store.saved_field["decomposition"]["removed_indices"] == [2]
    assert store.prepared is True

class CandidateStore:
    def __init__(self):
        self.candidates = []
        self.jobs = []

    def pending_jobs(self, task_id): return 0
    def generation(self, task_id): return 0
    def enqueue_pipeline_job(self, job): self.jobs.append(dict(job)); return True
    def save_candidate(self, task_id, candidate_id, value): self.candidates.append(dict(value))


def test_whole_candidate_identity_and_payload_include_overlay():
    store = CandidateStore()
    workflow = PipelineWorkflow(store, Settings())
    token = workflow._overlay_context.set(44)
    try:
        workflow._queue_whole_layout("task", {"n": 2, "is_feasible": True, "is_optimal": True}, variant="smooth")
    finally:
        workflow._overlay_context.reset(token)
    assert store.candidates[0]["overlay_id"] == 44
    assert store.jobs[0]["payload"]["overlay_id"] == 44


class LayoutStore:
    def load_field(self, task_id, *, variant="raw", overlay_id=0):
        from shapely.geometry import box
        return {
            "cfg": {"back_grid": (18, 300), "axis": "y", "rebar_config_source": "test"},
            "start_polygons": [{"geometry": box(0, 0, 10, 10)}],
        }
    def get_meta(self, task_id): return {"parameters": {"steel_density_kg_m3": 7850.0}, "requested_n": [1]}


def test_layout_solution_preserves_overlay_and_optimal_status(monkeypatch):
    import A101.rebar_field_layout as layout_module
    import A101.reinforcement_components as components_module
    monkeypatch.setattr(layout_module, "layout_rebars", lambda **kwargs: {"is_feasible": True, "bars": []})
    monkeypatch.setattr(components_module, "bar_mass_kg", lambda bars, density: 12.0)
    store = LayoutStore()
    workflow = PipelineWorkflow(store, Settings())
    token = workflow._overlay_context.set(123)
    try:
        solution = workflow._layout_candidate("task", {
            "candidate_id": "c", "variant": "smooth", "source": "components", "total_N": 2,
            "component_ns": {"0": 2}, "component_choices": {"0": {"is_optimal": True}},
            "anchored_boxes": [], "overlay_id": 123,
        })
    finally:
        workflow._overlay_context.reset(token)
    assert solution["overlay_id"] == 123
    assert solution["is_optimal"] is True
    assert solution["status"] == "optimal"


def test_component_source_indices_are_mapped_back_to_stable_source_polygons():
    from shapely.geometry import box
    from rebar_service.pipeline import map_components_to_source_indices
    sources = [
        {"source_index": 3, "geometry": box(0, 0, 10, 10)},
        {"source_index": 56, "geometry": box(20, 0, 30, 10)},
    ]
    components = [
        {"id": 0, "demand_geometry": box(0, 0, 10, 10), "polygon_indices": [0]},
        {"id": 1, "demand_geometry": box(20, 0, 30, 10), "polygon_indices": [1]},
    ]
    map_components_to_source_indices(components, sources)
    assert components[0]["polygon_indices"] == [3]
    assert components[1]["polygon_indices"] == [56]


class WholeStableStore:
    def load_field(self, task_id, *, variant="raw", overlay_id=0):
        from shapely.geometry import box
        return {
            "cfg": {"axis": "y", "load2cls": {5.0: 1, 4.0: 1}, "holds": {1: 0.0}},
            "ortho_polygons": [
                {"geometry": box(0, 0, 10, 10), "load": 5.0},
                {"geometry": box(20, 0, 30, 10), "load": 4.0},
            ],
            "decomposition": {"active_indices": [3, 56]},
        }


def test_whole_component_public_polygon_indices_use_stable_source_indices():
    workflow = PipelineWorkflow(WholeStableStore(), Settings())
    component = workflow._whole_component("task", "raw")
    assert component["polygon_indices"] == [3, 56]
