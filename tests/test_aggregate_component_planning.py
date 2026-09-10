from __future__ import annotations

from collections import deque

import pytest

from rebar_service.config import Settings
from rebar_service.pipeline import JobKind, PipelineWorkflow


def test_aggregate_local_n_requirements_uses_sum_ranges_and_reports_unreachable_totals():
    import rebar_service.pipeline as pipeline

    helper = getattr(pipeline, "aggregate_local_n_requirements", None)
    assert callable(helper), "aggregate_local_n_requirements must exist"

    plans, unreachable = helper({0: 7, 1: 11}, [1, 10, 12, 19])

    assert unreachable == [1, 19]
    assert set(plans[0]) == set(range(1, 8))
    assert set(plans[1]) == set(range(3, 12))


def test_virtual_aggregate_info_sums_real_component_maxima_and_single_component_aliases_zero():
    class Store:
        def __init__(self, components):
            self.rows = {str(k): dict(v) for k, v in components.items()}

        def component_ids(self, task_id, **kwargs):
            return sorted(self.rows, key=int)

        def load_component(self, task_id, component_id, **kwargs):
            return self.rows.get(str(component_id))

    multi = PipelineWorkflow(
        Store({
            0: {"info": {"id": 0, "max_useful_n": 7, "prepared": True, "state": "prepared"}, "max_useful_n": 7, "max_n_state": "ready"},
            1: {"info": {"id": 1, "max_useful_n": 11, "prepared": True, "state": "prepared"}, "max_useful_n": 11, "max_n_state": "ready"},
        }),
        Settings(),
    )
    info = multi.aggregate_component_info("task")
    assert info["id"] == -1
    assert info["max_useful_n"] == 18
    assert info["prepared"] is True
    assert info["component_ids"] == [0, 1]

    single = PipelineWorkflow(
        Store({0: {"info": {"id": 0, "max_useful_n": 5, "prepared": True, "state": "prepared"}, "max_useful_n": 5, "max_n_state": "ready"}}),
        Settings(),
    )
    alias = single.aggregate_component_info("task")
    assert alias["id"] == -1
    assert alias["alias_component_id"] == 0
    assert alias["max_useful_n"] == 5


def test_empty_analysis_is_the_only_virtual_aggregate_that_reports_zero_max_n():
    class Store:
        def component_ids(self, task_id, **kwargs):
            return []

        def load_component(self, *args, **kwargs):
            return None

    info = PipelineWorkflow(Store(), Settings()).aggregate_component_info("task")
    assert info["max_useful_n"] == 0
    assert info["state"] == "empty"
    assert info["prepared"] is True


def test_minus_one_schedule_with_single_component_queues_real_component_job_only():
    class Store:
        def __init__(self):
            self.jobs = []

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "prepared"}

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-1]}

        def component_ids(self, task_id, **kwargs):
            return ["0"]

        def load_component(self, task_id, component_id, **kwargs):
            if str(component_id) == "0":
                return {"max_useful_n": 5, "max_n_state": "ready"}
            return None

        def pending_jobs(self, task_id):
            return 0

        def generation(self, task_id):
            return 0

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    queued = workflow.schedule_component_n("task", -1, [3])

    assert queued == [3]
    assert [job["kind"] for job in store.jobs] == [JobKind.solve_component.value]
    assert store.jobs[0]["payload"]["component_id"] == 0
    assert not any(job["kind"] in {JobKind.prepare_whole.value, JobKind.solve_whole.value} for job in store.jobs)


def test_local_infeasible_result_does_not_prevent_other_n_from_combining():
    from A101.reinforcement_components import combine_component_frontiers

    combined = combine_component_frontiers(
        {
            0: {
                2: {"is_feasible": True, "proxy_mass": 2.0, "anchored_boxes": [], "rectangles": []},
                3: {"is_feasible": False, "status": "infeasible"},
                4: {"is_feasible": True, "proxy_mass": 4.0, "anchored_boxes": [], "rectangles": []},
            },
            1: {
                6: {"is_feasible": True, "proxy_mass": 6.0, "anchored_boxes": [], "rectangles": []},
                7: {"is_feasible": True, "proxy_mass": 7.0, "anchored_boxes": [], "rectangles": []},
                8: {"is_feasible": True, "proxy_mass": 8.0, "anchored_boxes": [], "rectangles": []},
            },
        },
        top_k=3,
    )

    assert 10 in combined
    assert 11 in combined
    assert all(row["component_ns"].get(0) != 3 for rows in combined.values() for row in rows)


def test_component_preparation_failure_never_publishes_zero_max_n():
    class Store:
        def __init__(self):
            self.saved = None
            self.events = []

        def save_component(self, task_id, component_id, value, **kwargs):
            self.saved = dict(value)

        def publish_event(self, task_id, event_type, payload, **kwargs):
            self.events.append((event_type, dict(payload)))
            return "1"

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow._maybe_complete_analysis = lambda *args, **kwargs: False

    from rebar_service.pipeline import PipelineJob
    workflow._record_cover_infeasible(
        PipelineJob("prepare_component", "task", {"component_id": 0, "analysis_auto_solve": True}),
        0,
        {"component": {"id": 0}},
        RuntimeError("cover failed"),
    )

    assert store.saved["max_useful_n"] is None
    assert store.saved["max_n_milp"]["max_useful_n"] is None


def test_aggregate_marks_only_missing_total_infeasible_and_keeps_neighbor_totals():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.jobs = []
            self.statuses = {}
            self.candidates = {}
            self.events = []
            self.frontiers = {
                0: {
                    2: {"n": 2, "is_feasible": True, "proxy_mass": 2.0, "anchored_boxes": [], "rectangles": []},
                    3: {"n": 3, "is_feasible": False, "status": "infeasible"},
                    4: {"n": 4, "is_feasible": True, "proxy_mass": 4.0, "anchored_boxes": [], "rectangles": []},
                },
                1: {
                    7: {"n": 7, "is_feasible": False, "status": "infeasible"},
                    8: {"n": 8, "is_feasible": True, "proxy_mass": 8.0, "anchored_boxes": [], "rectangles": []},
                },
            }

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-1], "component_result_top_k": 3}

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def component_ids(self, task_id, **kwargs):
            return [0, 1]

        def load_component(self, task_id, component_id, **kwargs):
            return {"max_useful_n": 4 if int(component_id) == 0 else 8, "max_n_state": "ready"}

        def frontier_version(self, task_id, **kwargs):
            return 5

        def load_frontier(self, task_id, component_id, **kwargs):
            return self.frontiers[int(component_id)]

        def requested_ns(self, task_id, **kwargs):
            return [10, 11, 12]

        def save_candidate(self, task_id, candidate_id, value):
            self.candidates[candidate_id] = dict(value)

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def set_n_status(self, task_id, n, status, **kwargs):
            self.statuses[int(n)] = {"status": status, **kwargs}

        def get_n_statuses(self, task_id, **kwargs):
            return {n: dict(row) for n, row in self.statuses.items()}

        def publish_event(self, task_id, event_type, payload, **kwargs):
            self.events.append((event_type, dict(payload)))
            return "1"

    store = Store()
    workflow = PipelineWorkflow(store, Settings(combine_batch_size=20))
    workflow.handle_combine_frontiers(PipelineJob("combine_frontiers", "task", {"frontier_version": 5}))

    totals = sorted(candidate["total_N"] for candidate in store.candidates.values())
    assert totals == [10, 12]
    assert store.statuses[11]["status"] == "infeasible"
    assert store.statuses[11]["reason"] == "no_feasible_component_combination"
    assert 10 not in store.statuses
    assert 12 not in store.statuses


def test_task_component_selector_is_validated_after_decomposition_before_any_worker_job():
    from tests.support.memory_scene_store import MemoryStore

    store = MemoryStore("x")
    store.meta["component_selection"] = [99]
    workflow = PipelineWorkflow(store, store.settings)

    with pytest.raises(ValueError, match="Unknown component ids: \\[99\\]"):
        workflow.prepare_task_components("task", auto_solve=True)

    assert store.components == {}
    assert store.enqueued == []


def test_virtual_aggregate_reports_structural_component_preparation_failure_without_zero_max():
    class Store:
        def component_ids(self, task_id, **kwargs):
            return [0, 1]

        def load_component(self, task_id, component_id, **kwargs):
            if int(component_id) == 0:
                return {"info": {"id": 0}, "max_useful_n": 5, "max_n_state": "ready"}
            return {"info": {"id": 1}, "max_useful_n": None, "max_n_state": "infeasible"}

    info = PipelineWorkflow(Store(), Settings()).aggregate_component_info("task")

    assert info["prepared"] is False
    assert info["state"] == "infeasible"
    assert info["max_useful_n"] is None
