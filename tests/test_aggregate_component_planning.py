from __future__ import annotations

from collections import deque

import pytest

from rebar_service.config import Settings
from rebar_service.pipeline import JobKind, PipelineWorkflow


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


def test_aggregate_emits_all_reachable_totals_without_mutating_requested_local_n_statuses():
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
            return {"scene_id": "scene", "component_selection": [-3], "component_result_top_k": 3}

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
    assert store.statuses == {}


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


def test_requested_n_is_local_per_component_and_not_an_aggregate_total_filter():
    class Store:
        def __init__(self):
            self.jobs = []
            self.statuses = []

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-3], "scan_mode": "requested"}

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "prepared"}

        def ensure_analysis(self, *args, **kwargs):
            return None

        def add_requested_ns(self, *args, **kwargs):
            return None

        def component_ids(self, *args, **kwargs):
            return [0, 1, 2]

        def load_component(self, task_id, component_id, **kwargs):
            maxima = {0: 62, 1: 2, 2: 4}
            return {"max_useful_n": maxima[int(component_id)], "max_n_state": "ready"}

        def completed_frontier_ns(self, *args, **kwargs):
            return set()

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def set_n_status(self, *args, **kwargs):
            self.statuses.append((args, kwargs))

    store = Store()
    workflow = PipelineWorkflow(store, Settings())

    queued = workflow.schedule_requested_for_all("task", [1, 2, 3, 4, 5])

    assert queued == {
        "0": [1, 5, 2, 4, 3],
        "1": [1, 2],
        "2": [1, 4, 2, 3],
    }
    actual = {
        cid: sorted(job["payload"]["n"] for job in store.jobs if job["payload"].get("component_id") == cid)
        for cid in (0, 1, 2)
    }
    assert actual == {0: [1, 2, 3, 4, 5], 1: [1, 2], 2: [1, 2, 3, 4]}
    assert all(job["kind"] == JobKind.solve_component.value for job in store.jobs)
    assert store.statuses == []


def test_minus_two_schedules_components_and_direct_whole_for_same_local_n_values():
    class Store:
        def __init__(self):
            self.jobs = []

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-2], "scan_mode": "requested"}

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "prepared"}

        def ensure_analysis(self, *args, **kwargs):
            pass

        def add_requested_ns(self, *args, **kwargs):
            pass

        def component_ids(self, *args, **kwargs):
            return [0, 1, "whole"]

        def load_component(self, task_id, component_id, **kwargs):
            if str(component_id) == "whole":
                return {"max_useful_n": 3, "max_n_state": "ready"}
            return {"max_useful_n": 5 if int(component_id) == 0 else 2, "max_n_state": "ready"}

        def completed_frontier_ns(self, task_id, component_id, **kwargs):
            return set()

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    queued = workflow.schedule_requested_for_all("task", [1, 2, 3, 4, 5])

    assert queued["0"] == [1, 5, 2, 4, 3]
    assert queued["1"] == [1, 2]
    assert queued["whole"] == [1, 3, 2]
    assert any(job["kind"] == JobKind.solve_component.value for job in store.jobs)
    assert any(job["kind"] == JobKind.solve_whole.value for job in store.jobs)


def test_minus_one_schedules_only_direct_whole():
    class Store:
        def __init__(self):
            self.jobs = []

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-1], "scan_mode": "requested"}

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "prepared"}

        def ensure_analysis(self, *args, **kwargs):
            pass

        def add_requested_ns(self, *args, **kwargs):
            pass

        def component_ids(self, *args, **kwargs):
            return [0, 1, "whole"]

        def load_component(self, task_id, component_id, **kwargs):
            if str(component_id) == "whole":
                return {"max_useful_n": 4, "max_n_state": "ready"}
            return {"max_useful_n": 10, "max_n_state": "ready"}

        def completed_frontier_ns(self, *args, **kwargs):
            return set()

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    queued = workflow.schedule_requested_for_all("task", [1, 2, 3, 4, 5])

    assert queued == {"whole": [1, 4, 2, 3]}
    assert [job["kind"] for job in store.jobs] == [JobKind.solve_whole.value] * 4


def test_component_combination_outputs_all_reachable_totals_not_only_requested_local_n():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.jobs = []
            self.candidates = {}

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-3], "component_result_top_k": 2}

        def component_ids(self, *args, **kwargs):
            return [0, 1]

        def load_component(self, task_id, component_id, **kwargs):
            return {"max_useful_n": 5, "max_n_state": "ready"}

        def load_frontier(self, task_id, component_id, **kwargs):
            return {
                1: {"n": 1, "is_feasible": True, "proxy_mass": 10.0 + int(component_id), "anchored_boxes": [], "rectangles": []}
            }

        def frontier_version(self, *args, **kwargs):
            return 2

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def save_candidate(self, task_id, candidate_id, candidate):
            self.candidates[candidate_id] = dict(candidate)

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def publish_event(self, *args, **kwargs):
            return "1"

        def requested_ns(self, *args, **kwargs):
            return [1]

    store = Store()
    workflow = PipelineWorkflow(store, Settings(combine_batch_size=20))
    workflow.handle_combine_frontiers(PipelineJob("combine_frontiers", "task", {"frontier_version": 2}))

    assert {candidate["total_N"] for candidate in store.candidates.values()} == {2}
    assert any(job["kind"] == JobKind.layout_solution.value and job["payload"]["total_n"] == 2 for job in store.jobs)


def test_later_local_results_expand_aggregate_totals_without_changing_requested_local_plan():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.jobs = []
            self.candidates = {}
            self.frontiers = {
                0: {1: {"n": 1, "is_feasible": True, "proxy_mass": 5.0, "anchored_boxes": [], "rectangles": []}},
                1: {1: {"n": 1, "is_feasible": True, "proxy_mass": 6.0, "anchored_boxes": [], "rectangles": []}},
            }
            self.version = 2

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-3], "component_result_top_k": 2}

        def component_ids(self, *args, **kwargs):
            return [0, 1]

        def load_component(self, *args, **kwargs):
            return {"max_useful_n": 5, "max_n_state": "ready"}

        def load_frontier(self, task_id, component_id, **kwargs):
            return self.frontiers[int(component_id)]

        def frontier_version(self, *args, **kwargs):
            return self.version

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def save_candidate(self, task_id, candidate_id, candidate):
            self.candidates[candidate_id] = dict(candidate)

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def publish_event(self, *args, **kwargs):
            return "1"

        def requested_ns(self, *args, **kwargs):
            return [1, 2]

    store = Store()
    workflow = PipelineWorkflow(store, Settings(combine_batch_size=50))
    workflow.handle_combine_frontiers(PipelineJob("combine_frontiers", "task", {"frontier_version": 2}))
    assert {c["total_N"] for c in store.candidates.values()} == {2}

    store.frontiers[0][2] = {"n": 2, "is_feasible": True, "proxy_mass": 4.0, "anchored_boxes": [], "rectangles": []}
    store.version = 3
    workflow.handle_combine_frontiers(PipelineJob("combine_frontiers", "task", {"frontier_version": 3}))

    assert {c["total_N"] for c in store.candidates.values()} == {2, 3}


def test_scene_minus_two_prepares_real_components_and_direct_whole():
    from tests.support.memory_scene_store import MemoryStore

    store = MemoryStore("y")
    store.meta["component_selection"] = [-2]
    workflow = PipelineWorkflow(store, store.settings)

    field = workflow.prepare_task_components("task", auto_solve=True)
    kinds = [job["kind"] for job in store.enqueued]

    assert any(kind == JobKind.prepare_component.value for kind in kinds)
    assert JobKind.prepare_whole.value in kinds
    assert "whole" in field["expected_solver_units"]


def test_scene_minus_one_prepares_component_max_bounds_but_solves_only_direct_whole():
    from tests.support.memory_scene_store import MemoryStore

    store = MemoryStore("y")
    store.meta["component_selection"] = [-1]
    workflow = PipelineWorkflow(store, store.settings)

    field = workflow.prepare_task_components("task", auto_solve=True)
    prepare_component_jobs = [
        job for job in store.enqueued if job["kind"] == JobKind.prepare_component.value
    ]

    assert JobKind.prepare_whole.value in [job["kind"] for job in store.enqueued]
    assert prepare_component_jobs
    assert all(job["payload"]["analysis_auto_solve"] is False for job in prepare_component_jobs)
    assert field["expected_solver_units"] == ["whole"]


def test_component_starts_requested_local_solves_immediately_when_its_max_n_is_ready():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.jobs = []
            self.record = {"component": {"id": 0}}

        def load_component(self, *args, **kwargs):
            return dict(self.record)

        def save_component(self, task_id, component_id, value, **kwargs):
            self.record = dict(value)

        def requested_ns(self, *args, **kwargs):
            return [1, 2, 3, 4, 5]

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-3], "scan_mode": "requested"}

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def publish_event(self, *args, **kwargs):
            return "1"

    store = Store()
    workflow = PipelineWorkflow(store, Settings(scheduler_batch_size=20))
    workflow._compute_exact_max_n = lambda *a, **kw: {"feasible": True, "max_useful_n": 3}
    workflow._maybe_complete_analysis = lambda *a, **kw: False

    workflow.handle_compute_max_n_component(
        PipelineJob("compute_max_n_component", "task", {"component_id": 0, "analysis_auto_solve": True})
    )

    solves = [job for job in store.jobs if job["kind"] == JobKind.solve_component.value]
    assert sorted(job["payload"]["n"] for job in solves) == [1, 2, 3]


def test_whole_starts_requested_local_solves_immediately_when_its_max_n_is_ready():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.jobs = []
            self.record = {"component": {"id": -1}}

        def load_component(self, *args, **kwargs):
            return dict(self.record)

        def save_component(self, task_id, component_id, value, **kwargs):
            self.record = dict(value)

        def requested_ns(self, *args, **kwargs):
            return [1, 2, 3, 4, 5]

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-1], "scan_mode": "requested"}

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def publish_event(self, *args, **kwargs):
            return "1"

    store = Store()
    workflow = PipelineWorkflow(store, Settings(scheduler_batch_size=20))
    workflow._compute_exact_max_n = lambda *a, **kw: {"feasible": True, "max_useful_n": 4}
    workflow._maybe_complete_analysis = lambda *a, **kw: False

    workflow.handle_compute_max_n_whole(
        PipelineJob("compute_max_n_whole", "task", {"analysis_auto_solve": True})
    )

    solves = [job for job in store.jobs if job["kind"] == JobKind.solve_whole.value]
    assert sorted(job["payload"]["n"] for job in solves) == [1, 2, 3, 4]


def test_scene_analysis_completion_waits_for_expected_whole_and_does_not_reschedule_initial_ns():
    class Store:
        def __init__(self):
            self.analysis = {"preparation_state": "preparing"}
            self.marked = False
            self.events = []

        def analysis_state(self, *args, **kwargs):
            return dict(self.analysis)

        def mark_analysis_prepared(self, *args, **kwargs):
            self.analysis["preparation_state"] = "prepared"
            self.marked = True

        def load_field(self, *args, **kwargs):
            return {"expected_solver_units": ["whole"]}

        def component_ids(self, *args, **kwargs):
            return [0, 1, "whole"]

        def load_component(self, task_id, component_id, **kwargs):
            if str(component_id) == "whole":
                return {"max_n_state": "ready", "max_useful_n": 4}
            return {"max_n_state": "not_requested", "max_useful_n": None}

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-1]}

        def publish_event(self, task_id, event_type, payload, **kwargs):
            self.events.append((event_type, dict(payload)))
            return "1"

        def requested_ns(self, *args, **kwargs):
            return [1, 2, 3]

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow.schedule_requested_for_all = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("initial N must already be scheduled per unit"))

    assert workflow._maybe_complete_analysis("task", "raw", True) is True
    assert store.marked is True
    assert store.analysis["preparation_state"] == "prepared"


def test_component_aggregate_solution_updates_results_without_becoming_a_local_n_request_status():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.candidate = {
                "candidate_id": "c", "source": "components", "total_N": 30,
                "component_ns": {"0": 1, "1": 1}, "anchored_boxes": [],
                "variant": "raw", "overlay_id": 0,
            }
            self.solutions = {}
            self.status_calls = []
            self.events = []

        def load_candidate(self, *args, **kwargs):
            return dict(self.candidate)

        def save_solution(self, task_id, solution):
            self.solutions[solution["solution_id"]] = dict(solution)

        def best_solution(self, task_id, total_n, **kwargs):
            rows = [s for s in self.solutions.values() if s["total_N"] == total_n and s["is_feasible"]]
            return min(rows, key=lambda s: s["actual_mass_kg"]) if rows else None

        def set_n_status(self, *args, **kwargs):
            self.status_calls.append((args, kwargs))

        def publish_event(self, task_id, event_type, payload, **kwargs):
            self.events.append((event_type, dict(payload)))
            return "1"

        def get_meta(self, task_id):
            return {"validate_results": False}

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow._layout_candidate = lambda task_id, candidate: {
        **candidate, "solution_id": "s", "source": "components", "total_N": 30,
        "actual_mass_kg": 100.0, "is_feasible": True, "is_optimal": False,
        "status": "feasible", "variant": "raw", "overlay_id": 0,
    }

    workflow.handle_layout_solution(PipelineJob("layout_solution", "task", {"candidate_id": "c", "variant": "raw"}))

    assert store.status_calls == []
    assert any(event == "solution_available" and payload["total_N"] == 30 for event, payload in store.events)


def test_best_result_event_is_emitted_on_first_solution_and_only_on_later_improvement():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.candidates = {
                "a": {"candidate_id": "a", "source": "components", "total_N": 30, "mass": 100.0, "variant": "raw", "overlay_id": 0},
                "b": {"candidate_id": "b", "source": "components", "total_N": 30, "mass": 90.0, "variant": "raw", "overlay_id": 0},
                "c": {"candidate_id": "c", "source": "components", "total_N": 30, "mass": 110.0, "variant": "raw", "overlay_id": 0},
            }
            self.solutions = {}
            self.events = []

        def load_candidate(self, task_id, candidate_id, **kwargs):
            return dict(self.candidates[candidate_id])

        def save_solution(self, task_id, solution):
            self.solutions[solution["solution_id"]] = dict(solution)

        def best_solution(self, task_id, total_n, **kwargs):
            rows = [s for s in self.solutions.values() if s["total_N"] == total_n and s["is_feasible"]]
            return min(rows, key=lambda s: s["actual_mass_kg"]) if rows else None

        def publish_event(self, task_id, event_type, payload, **kwargs):
            self.events.append((event_type, dict(payload)))
            return "1"

        def get_meta(self, task_id):
            return {"validate_results": False}

        def set_n_status(self, *args, **kwargs):
            pytest.fail("aggregate totals must not mutate local request status")

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow._layout_candidate = lambda task_id, candidate: {
        **candidate, "solution_id": candidate["candidate_id"], "source": "components", "total_N": 30,
        "actual_mass_kg": candidate["mass"], "is_feasible": True, "is_optimal": False,
        "status": "feasible", "variant": "raw", "overlay_id": 0,
    }

    for candidate_id in ("a", "b", "c"):
        workflow.handle_layout_solution(PipelineJob("layout_solution", "task", {"candidate_id": candidate_id, "variant": "raw"}))

    updates = [payload for event, payload in store.events if event == "result_updated"]
    assert [row["actual_mass_kg"] for row in updates] == [100.0, 90.0]
    assert updates[0]["previous_mass_kg"] is None
    assert updates[1]["previous_mass_kg"] == 100.0


def test_minus_two_with_one_real_component_does_not_duplicate_direct_whole_jobs():
    class Store:
        def __init__(self):
            self.jobs = []

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-2], "scan_mode": "requested"}

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "prepared"}

        def ensure_analysis(self, *args, **kwargs):
            pass

        def add_requested_ns(self, *args, **kwargs):
            pass

        def component_ids(self, *args, **kwargs):
            return [0]

        def load_component(self, task_id, component_id, **kwargs):
            if int(component_id) == 0:
                return {"max_useful_n": 3, "max_n_state": "ready"}
            return None

        def completed_frontier_ns(self, *args, **kwargs):
            return set()

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    queued = workflow.schedule_requested_for_all("task", [1, 2, 3])

    assert queued == {"0": [1, 3, 2]}
    assert all(job["kind"] == JobKind.solve_component.value for job in store.jobs)


def test_minus_one_with_one_real_component_uses_component_zero_as_whole_alias():
    class Store:
        def __init__(self):
            self.jobs = []

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-1], "scan_mode": "requested"}

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "prepared"}

        def ensure_analysis(self, *args, **kwargs):
            pass

        def add_requested_ns(self, *args, **kwargs):
            pass

        def component_ids(self, *args, **kwargs):
            return [0]

        def load_component(self, task_id, component_id, **kwargs):
            if int(component_id) == 0:
                return {"max_useful_n": 3, "max_n_state": "ready"}
            return None

        def completed_frontier_ns(self, *args, **kwargs):
            return set()

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    queued = workflow.schedule_requested_for_all("task", [1, 2, 3])

    assert queued == {"0": [1, 3, 2]}
    assert all(job["kind"] == JobKind.solve_component.value for job in store.jobs)


def test_new_local_n_request_schedules_already_ready_units_while_other_units_are_still_preparing():
    class Store:
        def __init__(self):
            self.jobs = []

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-3], "scan_mode": "requested"}

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "preparing"}

        def ensure_analysis(self, *args, **kwargs):
            pass

        def add_requested_ns(self, *args, **kwargs):
            pass

        def load_field(self, *args, **kwargs):
            return {"expected_solver_units": [0, 1]}

        def component_ids(self, *args, **kwargs):
            return [0, 1]

        def load_component(self, task_id, component_id, **kwargs):
            if int(component_id) == 0:
                return {"max_useful_n": 10, "max_n_state": "ready"}
            return {"max_useful_n": None, "max_n_state": "queued"}

        def completed_frontier_ns(self, *args, **kwargs):
            return set()

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    queued = workflow.schedule_requested_for_all("task", [5])

    assert queued == {"0": [5]}
    assert [(job["kind"], job["payload"]["component_id"], job["payload"]["n"]) for job in store.jobs] == [
        (JobKind.solve_component.value, 0, 5)
    ]


def test_scene_component_minus_one_schedules_direct_whole_local_n_not_aggregate_component_totals():
    class Store:
        def __init__(self):
            self.jobs = []

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "prepared"}

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-1]}

        def component_ids(self, task_id, **kwargs):
            return [0, 1, "whole"]

        def load_component(self, task_id, component_id, **kwargs):
            if str(component_id) == "whole":
                return {"max_useful_n": 5, "max_n_state": "ready"}
            return {"max_useful_n": 20, "max_n_state": "ready"}

        def completed_frontier_ns(self, *args, **kwargs):
            return set()

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def pending_jobs(self, task_id):
            return 0

        def generation(self, task_id):
            return 0

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    queued = workflow.schedule_component_n("task", -1, [2, 4])

    assert queued == [2, 4]
    assert [(job["kind"], job["payload"]["component_id"], job["payload"]["n"]) for job in store.jobs] == [
        (JobKind.solve_whole.value, "whole", 2),
        (JobKind.solve_whole.value, "whole", 4),
    ]


def test_thirty_components_with_local_n_one_emit_aggregate_total_thirty_even_when_requested_local_ns_are_one_to_five():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.jobs = []
            self.candidates = {}
            self.status_calls = []

        def get_meta(self, task_id):
            return {
                "scene_id": "scene",
                "component_selection": [-3],
                "component_result_top_k": 1,
                "requested_n": [1, 2, 3, 4, 5],
            }

        def component_ids(self, task_id, **kwargs):
            return list(range(30))

        def load_component(self, task_id, component_id, **kwargs):
            return {"max_useful_n": 5, "max_n_state": "ready"}

        def load_frontier(self, task_id, component_id, **kwargs):
            return {
                1: {
                    "n": 1,
                    "is_feasible": True,
                    "proxy_mass": float(int(component_id) + 1),
                    "anchored_boxes": [],
                    "rectangles": [],
                }
            }

        def frontier_version(self, task_id, **kwargs):
            return 30

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def save_candidate(self, task_id, candidate_id, candidate):
            self.candidates[candidate_id] = dict(candidate)

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def publish_event(self, *args, **kwargs):
            return "1"

        def set_n_status(self, *args, **kwargs):
            self.status_calls.append((args, kwargs))

    store = Store()
    workflow = PipelineWorkflow(store, Settings(combine_batch_size=100))
    workflow.handle_combine_frontiers(PipelineJob("combine_frontiers", "task", {"frontier_version": 30}))

    assert {candidate["total_N"] for candidate in store.candidates.values()} == {30}
    assert any(
        job["kind"] == JobKind.layout_solution.value and job["payload"]["total_n"] == 30
        for job in store.jobs
    )
    assert store.status_calls == []


def test_later_local_n_extension_skips_completed_values_and_caps_each_component_at_its_own_maximum():
    class Store:
        def __init__(self):
            self.jobs = []

        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-3], "scan_mode": "requested"}

        def analysis_state(self, *args, **kwargs):
            return {"preparation_state": "prepared"}

        def ensure_analysis(self, *args, **kwargs):
            pass

        def add_requested_ns(self, *args, **kwargs):
            pass

        def component_ids(self, *args, **kwargs):
            return [0, 1]

        def load_component(self, task_id, component_id, **kwargs):
            return {
                "max_useful_n": 7 if int(component_id) == 0 else 5,
                "max_n_state": "ready",
            }

        def completed_frontier_ns(self, task_id, component_id, **kwargs):
            return {5} if int(component_id) == 0 else set()

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def is_n_cancelled(self, *args, **kwargs):
            return False

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    queued = workflow.schedule_requested_for_all("task", list(range(5, 11)))

    assert queued == {"0": [5, 7, 6], "1": [5]}
    jobs = [(job["payload"]["component_id"], job["payload"]["n"]) for job in store.jobs]
    assert sorted(jobs) == [(0, 6), (0, 7), (1, 5)]


def test_direct_whole_result_can_be_replaced_by_lighter_component_aggregate_for_same_total_n():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.candidates = {
                "whole-c": {"candidate_id": "whole-c", "source": "whole", "total_N": 3, "mass": 100.0, "variant": "raw", "overlay_id": 0},
                "parts-c": {"candidate_id": "parts-c", "source": "components", "total_N": 3, "mass": 90.0, "variant": "raw", "overlay_id": 0},
            }
            self.solutions = {}
            self.events = []
            self.status_calls = []

        def load_candidate(self, task_id, candidate_id, **kwargs):
            return dict(self.candidates[candidate_id])

        def save_solution(self, task_id, solution):
            self.solutions[solution["solution_id"]] = dict(solution)

        def best_solution(self, task_id, total_n, **kwargs):
            rows = [s for s in self.solutions.values() if s["total_N"] == total_n and s["is_feasible"]]
            return min(rows, key=lambda row: row["actual_mass_kg"]) if rows else None

        def set_n_status(self, *args, **kwargs):
            self.status_calls.append((args, kwargs))

        def publish_event(self, task_id, event_type, payload, **kwargs):
            self.events.append((event_type, dict(payload)))
            return "1"

        def get_meta(self, task_id):
            return {"validate_results": False}

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow._layout_candidate = lambda task_id, candidate: {
        **candidate,
        "solution_id": candidate["candidate_id"],
        "actual_mass_kg": candidate["mass"],
        "is_feasible": True,
        "is_optimal": False,
        "status": "feasible",
        "variant": "raw",
        "overlay_id": 0,
    }

    workflow.handle_layout_solution(PipelineJob("layout_solution", "task", {"candidate_id": "whole-c", "variant": "raw"}))
    workflow.handle_layout_solution(PipelineJob("layout_solution", "task", {"candidate_id": "parts-c", "variant": "raw"}))

    best = store.best_solution("task", 3)
    assert best["source"] == "components"
    assert best["actual_mass_kg"] == 90.0
    updates = [payload for event, payload in store.events if event == "result_updated"]
    assert [(row["source"], row["actual_mass_kg"]) for row in updates] == [("whole", 100.0), ("components", 90.0)]
    assert len(store.status_calls) == 1
    assert store.status_calls[0][0][1:3] == (3, "feasible")


def test_structural_whole_max_n_failure_keeps_public_max_null_not_zero():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.record = {"component": {"id": -1}}
            self.jobs = []

        def load_component(self, *args, **kwargs):
            return dict(self.record)

        def save_component(self, task_id, component_id, value, **kwargs):
            self.record = dict(value)

        def requested_ns(self, *args, **kwargs):
            return [1, 2, 3]

        def publish_event(self, *args, **kwargs):
            return "1"

        def generation(self, task_id):
            return 0

        def pending_jobs(self, task_id):
            return 0

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow._compute_exact_max_n = lambda *a, **kw: {"feasible": False, "max_useful_n": 0, "reason": "test"}
    workflow._maybe_complete_analysis = lambda *a, **kw: False

    workflow.handle_compute_max_n_whole(
        PipelineJob("compute_max_n_whole", "task", {"component_id": "whole", "analysis_auto_solve": True})
    )

    assert store.record["max_useful_n"] is None
    assert store.record["max_n_milp"]["max_useful_n"] is None
    assert store.record["max_n_state"] == "infeasible"
    assert store.jobs == []


def test_single_component_whole_alias_detection_uses_current_variant_and_overlay_context():
    class Store:
        def get_meta(self, task_id):
            return {"scene_id": "scene", "component_selection": [-1]}

        def component_ids(self, task_id, *, variant="raw", overlay_id=0):
            if variant == "smooth" and overlay_id == 7:
                return [0]
            return [0, 1]

    workflow = PipelineWorkflow(Store(), Settings())
    assert workflow.aggregate_requested("task", variant="smooth", overlay_id=7) is True
    assert workflow.aggregate_requested("task", variant="raw", overlay_id=0) is False


def test_direct_whole_uses_sum_of_real_component_maxima_and_allows_n_below_component_count():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.jobs = []
            self.components = {
                **{
                    i: {
                        "component": {"id": i},
                        "max_useful_n": 1,
                        "max_n_state": "ready",
                        "state": "prepared",
                    }
                    for i in range(30)
                },
                "whole": {
                    "component": {"id": -1},
                    "max_useful_n": None,
                    "max_n_state": "queued",
                    "state": "max_n_queued",
                },
            }

        def get_meta(self, task_id):
            return {
                "scene_id": "scene",
                "component_selection": [-1],
                "requested_n": [1, 2, 3, 4, 5],
            }

        def component_ids(self, task_id, **kwargs):
            return list(range(30)) + ["whole"]

        def load_component(self, task_id, component_id, **kwargs):
            return dict(self.components[component_id])

        def save_component(self, task_id, component_id, record, **kwargs):
            self.components[component_id] = dict(record)

        def pending_jobs(self, task_id):
            return 0

        def generation(self, task_id):
            return 0

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def publish_event(self, *args, **kwargs):
            return "0-0"

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow._maybe_complete_analysis = lambda *args, **kwargs: True
    workflow._compute_exact_max_n = lambda *args, **kwargs: {
        "feasible": True,
        "max_useful_n": 2,
        "matrix_max_useful_n": 2,
    }

    workflow.handle_compute_max_n_whole(
        PipelineJob(
            JobKind.compute_max_n_whole.value,
            "task",
            {"analysis_auto_solve": True, "variant": "raw"},
        )
    )

    assert store.components["whole"]["max_useful_n"] == 30
    assert [
        job["payload"]["n"]
        for job in store.jobs
        if job["kind"] == JobKind.solve_whole.value
    ] == [1, 5, 2, 4, 3]


def test_last_bound_only_component_max_n_wakes_waiting_direct_whole_without_component_solves():
    from rebar_service.pipeline import PipelineJob

    class Store:
        def __init__(self):
            self.jobs = []
            self.components = {
                0: {"component": {"id": 0}, "max_useful_n": 1, "max_n_state": "ready", "state": "prepared"},
                1: {"component": {"id": 1}, "max_useful_n": None, "max_n_state": "queued", "state": "max_n_queued"},
                "whole": {
                    "component": {"id": -1}, "max_useful_n": None,
                    "max_n_state": "waiting_components", "state": "max_n_waiting",
                    "analysis_auto_solve": True,
                },
            }

        def get_meta(self, task_id):
            return {
                "scene_id": "scene",
                "component_selection": [-1],
                "requested_n": [1, 2],
            }

        def component_ids(self, task_id, **kwargs):
            return [0, 1, "whole"]

        def load_component(self, task_id, component_id, **kwargs):
            return dict(self.components[component_id])

        def save_component(self, task_id, component_id, record, **kwargs):
            self.components[component_id] = dict(record)

        def requested_ns(self, *args, **kwargs):
            return [1, 2]

        def pending_jobs(self, task_id):
            return 0

        def generation(self, task_id):
            return 0

        def enqueue_pipeline_job(self, job):
            self.jobs.append(dict(job))
            return True

        def publish_event(self, *args, **kwargs):
            return "0-0"

    store = Store()
    workflow = PipelineWorkflow(store, Settings())
    workflow._compute_exact_max_n = lambda *args, **kwargs: {
        "feasible": True, "max_useful_n": 1, "matrix_max_useful_n": 1,
    }
    workflow._maybe_complete_analysis = lambda *args, **kwargs: True

    workflow.handle_compute_max_n_component(
        PipelineJob(
            JobKind.compute_max_n_component.value,
            "task",
            {
                "component_id": 1,
                "variant": "raw",
                "analysis_auto_solve": False,
                "whole_bound_only": True,
            },
        )
    )

    assert store.components["whole"]["max_useful_n"] == 2
    assert not [job for job in store.jobs if job["kind"] == JobKind.solve_component.value]
    assert [
        job["payload"]["n"] for job in store.jobs if job["kind"] == JobKind.solve_whole.value
    ] == [1, 2]
