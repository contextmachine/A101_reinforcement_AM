from rebar_service.config import Settings
from rebar_service.pipeline import JobKind, PipelineJob, PipelineWorkflow


class _FitRecoveryStore:
    def __init__(self, *, frontier=None, problem=True, solver=None):
        self.frontier = frontier or {}
        self.problem = {"problem": {}} if problem else None
        self.solver = solver
        self.jobs = []
        self.events = []
        self.deleted = []

    def generation(self, task_id):
        return 0

    def pending_jobs(self, task_id):
        return 0

    def enqueue_pipeline_job(self, job):
        self.jobs.append(dict(job))
        return True

    def load_frontier(self, task_id, component_id, **kwargs):
        return dict(self.frontier)

    def load_problem(self, task_id, component_id, **kwargs):
        return self.problem

    def load_solver_result(self, task_id, component_id, n, **kwargs):
        return self.solver

    def frontier_version(self, task_id, **kwargs):
        return 7

    def publish_event(self, task_id, event_type, payload, **kwargs):
        self.events.append((event_type, dict(payload)))
        return "1"

    def delete_solver_result(self, task_id, component_id, n, **kwargs):
        self.deleted.append((component_id, n))


def test_fit_replay_with_existing_frontier_resumes_downstream_without_solver_artifact():
    store = _FitRecoveryStore(frontier={2: {"n": 2, "is_feasible": True, "is_optimal": True}})
    workflow = PipelineWorkflow(store, Settings())

    workflow.handle_fit_component(PipelineJob("fit_component", "task", {"component_id": 3, "n": 2, "variant": "raw"}))

    assert any(job["kind"] == JobKind.combine_frontiers.value for job in store.jobs)
    assert store.deleted == [(3, 2)]


def test_fit_replay_with_missing_solver_artifact_requeues_solve_instead_of_raising():
    store = _FitRecoveryStore(frontier={}, problem=True, solver=None)
    workflow = PipelineWorkflow(store, Settings())

    workflow.handle_fit_component(PipelineJob("fit_component", "task", {"component_id": 3, "n": 2, "variant": "raw"}))

    assert [job["kind"] for job in store.jobs] == [JobKind.solve_component.value]
    assert store.jobs[0]["payload"]["component_id"] == 3
    assert store.jobs[0]["payload"]["n"] == 2
