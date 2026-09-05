from rebar_service import api
from rebar_service.models import TaskParameters


class _CaptureStore:
    def __init__(self):
        self.meta = None

    def create_task(self, task_id, meta, plan, input_obj):
        self.meta = dict(meta)

    def publish_event(self, task_id, event_type, payload):
        return "1"


class _NoopWorkflow:
    def bootstrap_task(self, task_id, smooth=False):
        return True


def test_task_parameters_json_does_not_duplicate_task_control_columns(monkeypatch):
    store = _CaptureStore()
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "workflow", _NoopWorkflow())

    parameters = TaskParameters(
        n=[1, 2],
        scan_mode="hard",
        whole=True,
        component_result_top_k=7,
        validate_results=True,
        max_concurrent_jobs=9,
    )
    api._build_task(
        parameters,
        {"kind": "polygons", "units": "mm", "polygons": [{"points": [(0, 0), (10, 0), (0, 10)], "load": 1.0}]},
        start_pipeline=False,
    )

    assert store.meta is not None
    for duplicated in (
        "n",
        "scan_mode",
        "whole",
        "component_result_top_k",
        "validate_results",
        "max_concurrent_jobs",
    ):
        assert duplicated not in store.meta["parameters"]
    assert store.meta["scan_mode"] == "hard"
    assert store.meta["whole"] is True
    assert store.meta["component_result_top_k"] == 7
    assert store.meta["validate_results"] is True
    assert store.meta["max_concurrent_jobs"] == 9
