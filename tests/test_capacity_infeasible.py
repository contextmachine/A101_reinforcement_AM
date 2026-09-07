import A101.calculate_mass as calculate_mass


def test_capacity_error_type_is_domain_specific():
    assert hasattr(calculate_mass, "ReinforcementCapacityError")
    try:
        calculate_mass.make_rebar_classes([10_000.0], (18, 300), [(20, 150)], max_lay=1)
    except calculate_mass.ReinforcementCapacityError as exc:
        assert exc.load == 10_000.0
        assert exc.max_supported_load > 0
    else:
        raise AssertionError("capacity overflow must raise ReinforcementCapacityError")

from rebar_service.config import Settings
from rebar_service.pipeline import PipelineJob, PipelineWorkflow


class _CapacityStore:
    def __init__(self):
        self.events = []
        self.statuses = []
        self.analysis = None
        self.meta = {
            "task_id": "task",
            "parameters": {
                "back_grid": [18, 300],
                "stock": [[20, 150]],
                "max_layers": 1,
                "axis": "y",
            },
            "requested_n": [1, 2],
            "initial_variant": "raw",
            "manual_mode": False,
        }

    def patch_meta(self, task_id, **changes):
        self.meta.update(changes)
        return dict(self.meta)

    def get_meta(self, task_id):
        return dict(self.meta)

    def publish_event(self, task_id, event_type, payload, **kwargs):
        self.events.append((event_type, dict(payload)))
        return "1"

    def get_object(self, task_id, name):
        return {"kind": "polygons", "units": "mm", "polygons": self.load_variant_polygons(task_id)}

    def load_variant_polygons(self, task_id, *, variant="raw"):
        return [{"points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]], "load": 10_000.0}]

    def requested_ns(self, task_id, *, variant="raw", overlay_id=0):
        return [1, 2]

    def mark_analysis_infeasible(self, task_id, *, variant="raw", overlay_id=0, detail=None):
        self.analysis = (variant, overlay_id, dict(detail or {}))

    def set_n_status(self, task_id, n, status, *, variant=None, overlay_id=0, **extra):
        self.statuses.append((n, status, dict(extra)))


def test_prepare_capacity_shortfall_is_recorded_as_infeasible_not_exception():
    store = _CapacityStore()
    workflow = PipelineWorkflow(store, Settings())

    workflow.handle_prepare_field(PipelineJob("prepare_field", "task", {"auto_solve": True, "variant": "raw"}))

    assert store.analysis is not None
    assert store.analysis[2]["reason"] == "reinforcement_capacity"
    assert [row[:2] for row in store.statuses] == [(1, "infeasible"), (2, "infeasible")]
    assert any(event == "analysis_infeasible" for event, _ in store.events)
    assert store.meta["state"] == "completed"
