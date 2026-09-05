from __future__ import annotations

from pathlib import Path

import pytest

from rebar_service.config import Settings
from rebar_service.pipeline import JobKind, PipelineJob, PipelineWorkflow, to_compat_result
from rebar_service.store import RedisStore


class QueueStore:
    def __init__(self):
        self.jobs: list[dict] = []
        self.records = {
            "whole": {
                "component": {"id": -1},
                "max_useful_n": 8,
                "state": "prepared",
            }
        }

    def load_component(self, task_id, component_id):
        return self.records.get(component_id)

    def pending_jobs(self, task_id):
        return 0

    def generation(self, task_id):
        return 0

    def enqueue_pipeline_job(self, job):
        self.jobs.append(dict(job))
        return True


class PrepareStore(QueueStore):
    def __init__(self):
        super().__init__()
        self.records = {
            2: {
                "component": {
                    "id": 2,
                    "polygon_indices": [4],
                    "classes": [1],
                    "loads": [10.0],
                },
                "state": "queued",
                "info": {"id": 2},
            }
        }
        self.saved = {}
        self.events = []

    def get_meta(self, task_id):
        return {"requested_n": [1, 2, 3], "scan_mode": "requested"}

    def save_component(self, task_id, component_id, value):
        self.saved[component_id] = dict(value)
        self.records[component_id] = dict(value)

    def publish_event(self, task_id, event_type, payload):
        self.events.append((event_type, dict(payload)))
        return "1-0"

    def is_n_cancelled(self, task_id, n):
        return False


class ManualStateStore:
    def __init__(self):
        self.meta = {"state": "preparing_components", "manual_mode": True, "cancelled": False, "paused": False}
        self.patched = None
        self.events = []

    def get_meta(self, task_id):
        return dict(self.meta)

    def pending_jobs(self, task_id):
        return 0

    def solutions(self, task_id):
        return []

    def load_field(self, task_id):
        return {"decomposition": {"components": [{"id": 0}]}}

    def patch_meta(self, task_id, **changes):
        self.meta.update(changes)
        self.patched = dict(changes)
        return dict(self.meta)

    def publish_event(self, task_id, event_type, payload):
        self.events.append((event_type, dict(payload)))


def test_compat_result_is_compact_and_does_not_embed_internal_component_payloads():
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
            0: {
                "solver_result": {"is_feasible": True, "chosen_counts": [0] * 10000},
                "fit_result": {"is_feasible": True, "debug_matrix": [1] * 10000},
            }
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
            "tracks": [1] * 10000,
        },
        "metadata": {"huge": [1] * 10000},
    }

    result = to_compat_result(solution)

    assert "component_results" not in result["solver_result"]
    assert "component_results" not in result["fit_result"]
    assert "bar_layout" not in result
    assert "metadata" not in result
    assert "rectangles" not in result
    assert "anchored_boxes" not in result
    assert result["solver_result"]["component_ns"] == {"0": 1, "1": 2}
    assert result["fit_result"]["zones"][0]["class"] == 1
    assert result["summary"]["mass"] == 10.25


def test_component_minus_one_schedules_the_prepared_whole_component():
    store = QueueStore()
    workflow = PipelineWorkflow(store, Settings())

    queued = workflow.schedule_component_n("task", -1, [2, 4])

    assert queued == [2, 4]
    assert [job["kind"] for job in store.jobs] == [JobKind.solve_whole.value, JobKind.solve_whole.value]
    assert [job["payload"]["component_id"] for job in store.jobs] == ["whole", "whole"]
    assert [job["payload"]["source"] for job in store.jobs] == ["whole", "whole"]


def test_manual_component_preparation_does_not_auto_schedule_solver_jobs():
    store = PrepareStore()
    workflow = PipelineWorkflow(store, Settings())
    workflow._prepare_problem = lambda task_id, component_id, component: {  # type: ignore[method-assign]
        "max_useful_n": 5,
        "bounds": {"nonredundant_upper_bound": 5},
    }

    workflow.handle_prepare_component(
        PipelineJob("prepare_component", "task", {"component_id": 2, "auto_solve": False})
    )

    assert store.saved[2]["state"] == "prepared"
    assert store.jobs == []


def test_manual_task_without_pending_jobs_stays_ready_instead_of_becoming_error():
    store = ManualStateStore()

    result = RedisStore.refresh_pipeline_state(store, "task")

    assert result["state"] == "ready"
    assert store.patched == {"state": "ready"}


def test_api_declares_unified_upload_and_stateless_source_polygon_routes():
    root = Path(__file__).resolve().parents[1]
    source = (root / "rebar_service/api.py").read_text(encoding="utf-8")

    assert '@app.post("/v1/tasks/upload-only"' not in source
    assert '@app.post("/v1/tasks/upload"' in source
    assert '@app.post("/v1/source-polygons/upload"' in source
    assert "workflow.whole_component_info" in source


def test_component_frontier_has_no_implicit_timeout_default():
    root = Path(__file__).resolve().parents[1]
    source = (root / "A101/reinforcement_components.py").read_text(encoding="utf-8")

    marker = "def solve_component_frontier("
    start = source.index(marker)
    header = source[start : source.index(") ->", start)]
    assert "timeout: float | None = None" in header

class PrepareTaskStore(QueueStore):
    def __init__(self, manual_mode=True):
        super().__init__()
        self.manual_mode = manual_mode
        self.meta = {
            "manual_mode": manual_mode,
            "requested_n": [1, 2],
            "scan_mode": "requested",
            "whole": False,
            "state": "uploaded" if manual_mode else "created",
        }
        self.events = []

    def get_meta(self, task_id):
        return dict(self.meta)

    def patch_meta(self, task_id, **changes):
        self.meta.update(changes)
        return dict(self.meta)

    def publish_event(self, task_id, event_type, payload):
        self.events.append((event_type, dict(payload)))
        return "1-0"


def test_prepare_task_uses_manual_mode_to_prepare_without_auto_solve():
    store = PrepareTaskStore(manual_mode=True)
    workflow = PipelineWorkflow(store, Settings())

    queued = workflow.prepare_task("task")

    assert queued is True
    assert len(store.jobs) == 1
    assert store.jobs[0]["kind"] == JobKind.prepare_field.value
    assert store.jobs[0]["payload"]["auto_solve"] is False

class WholePrepareStore(QueueStore):
    def __init__(self):
        super().__init__()
        self.records = {}
        self.saved = {}

    def save_component(self, task_id, component_id, value):
        self.saved[component_id] = dict(value)
        self.records[component_id] = dict(value)

    def get_meta(self, task_id):
        return {"requested_n": [1, 2], "scan_mode": "requested"}

    def is_n_cancelled(self, task_id, n):
        return False


def test_manual_whole_preparation_does_not_auto_schedule_solver_jobs():
    store = WholePrepareStore()
    workflow = PipelineWorkflow(store, Settings())
    component = {
        "id": -1,
        "polygon_indices": [0, 1],
        "classes": [1, 2],
        "loads": [10.0, 20.0],
        "bounds": (0, 0, 10, 10),
        "demand_bounds": (0, 0, 10, 10),
    }
    workflow._whole_component = lambda task_id: component  # type: ignore[method-assign]
    workflow._prepare_problem = lambda task_id, component_id, value: {  # type: ignore[method-assign]
        "max_useful_n": 6,
        "bounds": {"nonredundant_upper_bound": 6},
    }

    workflow.handle_prepare_whole(PipelineJob("prepare_whole", "task", {"auto_solve": False}))

    assert store.saved["whole"]["state"] == "prepared"
    assert store.saved["whole"]["info"]["id"] == -1
    assert store.jobs == []

class FieldPrepareStore(QueueStore):
    def __init__(self):
        super().__init__()
        self.records = {}
        self.saved_components = {}
        self.saved_field = None
        self.meta = {
            "whole": False,
            "manual_mode": True,
            "parameters": {
                "back_grid": (18, 300),
                "stock": [(18, 300), (20, 150)],
                "max_layers": 2,
                "axis": "y",
                "anchor_factor": 32.0,
            },
        }
        self.events = []

    def patch_meta(self, task_id, **changes):
        self.meta.update(changes)
        return dict(self.meta)

    def publish_event(self, task_id, event_type, payload):
        self.events.append((event_type, dict(payload)))
        return "1-0"

    def get_object(self, task_id, name):
        return {"kind": "dxf", "filename": "manual.dxf", "content": b"dummy"}

    def get_meta(self, task_id):
        return dict(self.meta)

    def save_field(self, task_id, field):
        self.saved_field = dict(field)

    def save_component(self, task_id, component_id, value):
        self.saved_components[component_id] = dict(value)
        self.records[component_id] = dict(value)


def test_manual_field_preparation_queues_component_and_whole_preparation_without_auto_solve(monkeypatch):
    from shapely.geometry import Polygon
    import A101.axis_orientation as axis_orientation
    import A101.calculate_mass as calculate_mass
    import A101.grid_work as grid_work
    import A101.poly_bbox as poly_bbox
    import A101.reinforcement_components as reinforcement_components
    import rebar_service.pipeline as pipeline_module

    geometry = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
    source_polygon = {"geometry": geometry, "points": list(geometry.exterior.coords), "load": 10.0}
    component = {
        "id": 0,
        "polygon_indices": [0],
        "classes": [1],
        "loads": [10.0],
        "geometry": geometry,
        "demand_geometry": geometry,
        "bounds": geometry.bounds,
        "demand_bounds": geometry.bounds,
    }

    monkeypatch.setattr(pipeline_module, "polygons_from_input", lambda payload: [source_polygon])
    monkeypatch.setattr(axis_orientation, "normalize_axis", lambda value: "y")
    monkeypatch.setattr(axis_orientation, "class_holds", lambda diameters, recipes, anchor_factor: ({1: 0}, {1: 0}, {}))
    monkeypatch.setattr(
        calculate_mass,
        "make_rebar_classes",
        lambda loads, back_grid, stock, max_lay: {
            "load2cls": {10.0: 1, "10.0": 1},
            "diameters": {0: 18, 1: 20},
            "recipes": {},
            "densities": {0: 1.0, 1: 2.0},
            "steps": {0: 300, 1: 150},
        },
    )
    monkeypatch.setattr(poly_bbox, "rect_polygons", lambda polygons: polygons)
    monkeypatch.setattr(grid_work, "clean_poly", lambda polygons: polygons)
    monkeypatch.setattr(
        reinforcement_components,
        "split_reinforcement_components",
        lambda *args, **kwargs: {
            "components": [component],
            "active_indices": [0],
            "background_only_indices": [],
            "degenerate_indices": [],
        },
    )

    store = FieldPrepareStore()
    workflow = PipelineWorkflow(store, Settings())
    workflow.handle_prepare_field(PipelineJob("prepare_field", "task", {"auto_solve": False}))

    kinds = [job["kind"] for job in store.jobs]
    assert kinds == [JobKind.prepare_component.value, JobKind.prepare_whole.value]
    assert store.jobs[0]["payload"] == {"component_id": 0, "auto_solve": False}
    assert store.jobs[1]["payload"] == {"auto_solve": False}

class LazyWholeStore(QueueStore):
    def __init__(self):
        super().__init__()
        self.records = {}

    def load_field(self, task_id):
        return {"cfg": {}, "decomposition": {"components": [{"id": 0}]}}


def test_component_minus_one_lazily_prepares_whole_when_task_field_already_exists():
    store = LazyWholeStore()
    workflow = PipelineWorkflow(store, Settings())

    queued = workflow.schedule_component_n("task", -1, [3, 5])

    assert queued == [3, 5]
    assert len(store.jobs) == 1
    assert store.jobs[0]["kind"] == JobKind.prepare_whole.value
    assert store.jobs[0]["payload"] == {
        "auto_solve": True,
        "requested_n": [3, 5],
    }


def test_explicit_whole_prepare_schedules_only_the_requested_n_values():
    store = WholePrepareStore()
    workflow = PipelineWorkflow(store, Settings())
    component = {
        "id": -1,
        "polygon_indices": [0, 1],
        "classes": [1, 2],
        "loads": [10.0, 20.0],
        "bounds": (0, 0, 10, 10),
        "demand_bounds": (0, 0, 10, 10),
    }
    workflow._whole_component = lambda task_id: component  # type: ignore[method-assign]
    workflow._prepare_problem = lambda task_id, component_id, value: {  # type: ignore[method-assign]
        "max_useful_n": 6,
        "bounds": {"nonredundant_upper_bound": 6},
    }

    workflow.handle_prepare_whole(
        PipelineJob(
            "prepare_whole",
            "task",
            {"auto_solve": True, "requested_n": [3, 5]},
        )
    )

    assert [job["payload"]["n"] for job in store.jobs] == [3, 5]

def test_whole_component_info_is_available_as_minus_one_before_whole_problem_is_prepared():
    store = LazyWholeStore()
    workflow = PipelineWorkflow(store, Settings())
    workflow._whole_component = lambda task_id: {  # type: ignore[method-assign]
        "id": -1,
        "polygon_indices": [0, 1],
        "classes": [1, 2],
        "loads": [10.0, 20.0],
        "bounds": (0, 0, 10, 10),
        "demand_bounds": (0, 0, 10, 10),
    }

    info = workflow.whole_component_info("task")

    assert info["id"] == -1
    assert info["prepared"] is False
    assert info["state"] == "available"


def test_lazy_whole_invalid_n_still_leaves_whole_prepared_for_inspection():
    import pytest

    store = WholePrepareStore()
    workflow = PipelineWorkflow(store, Settings())
    component = {
        "id": -1,
        "polygon_indices": [0],
        "classes": [1],
        "loads": [10.0],
        "bounds": (0, 0, 10, 10),
        "demand_bounds": (0, 0, 10, 10),
    }
    workflow._whole_component = lambda task_id: component  # type: ignore[method-assign]
    workflow._prepare_problem = lambda task_id, component_id, value: {  # type: ignore[method-assign]
        "max_useful_n": 6,
        "bounds": {"nonredundant_upper_bound": 6},
    }

    with pytest.raises(ValueError, match="1..6"):
        workflow.handle_prepare_whole(
            PipelineJob("prepare_whole", "task", {"auto_solve": True, "requested_n": [99]})
        )

    assert store.saved["whole"]["state"] == "prepared"
    assert store.saved["whole"]["max_useful_n"] == 6


def test_api_uses_one_upload_route_and_one_component_prepare_route():
    root = Path(__file__).resolve().parents[1]
    source = (root / "rebar_service/api.py").read_text(encoding="utf-8")

    assert '@app.post("/v1/tasks/upload-only"' not in source
    assert '@app.post("/v1/tasks/{task_id}/component-pipeline/prepare"' not in source
    assert source.count('@app.post("/v1/tasks/{task_id}/components/prepare"') == 1
    assert 'start: bool = Query(True)' in source



def _import_api_without_redis():
    import importlib
    import sys
    from types import SimpleNamespace

    import rebar_service.store as store_module

    class FakeRedisClient:
        @classmethod
        def from_url(cls, *args, **kwargs):
            return object()

    store_module.redis = SimpleNamespace(Redis=FakeRedisClient)
    sys.modules.pop("rebar_service.api", None)
    return importlib.import_module("rebar_service.api")

def test_upload_config_parser_requires_config_only_when_starting():
    api_module = _import_api_without_redis()

    parser = getattr(api_module, "_parameters_from_upload_config", None)
    assert callable(parser), "_parameters_from_upload_config() must exist"

    manual = parser(None, start=False)
    assert manual.n == [1]

    partial_manual = parser('{"axis":"x"}', start=False)
    assert partial_manual.n == [1]
    assert partial_manual.axis == "x"

    with pytest.raises(ValueError, match="config.*required"):
        parser(None, start=True)

    automatic = parser('{"n":[2,3],"axis":"x"}', start=True)
    assert automatic.n == [2, 3]
    assert automatic.axis == "x"


def test_upload_source_mode_validation_is_exactly_one_mode():
    api_module = _import_api_without_redis()

    selector = getattr(api_module, "_upload_source_mode", None)
    assert callable(selector), "_upload_source_mode() must exist"

    assert selector(file_present=True, nodes_present=False, elements_present=False, loads_present=False) == "file"
    assert selector(file_present=False, nodes_present=True, elements_present=True, loads_present=True) == "xlsx"

    with pytest.raises(ValueError, match="source"):
        selector(file_present=False, nodes_present=False, elements_present=False, loads_present=False)
    with pytest.raises(ValueError, match="source"):
        selector(file_present=True, nodes_present=True, elements_present=True, loads_present=True)
    with pytest.raises(ValueError, match="all three XLSX"):
        selector(file_present=False, nodes_present=True, elements_present=False, loads_present=True)
