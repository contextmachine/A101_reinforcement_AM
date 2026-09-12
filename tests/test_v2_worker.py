from types import SimpleNamespace

from rebar_service.config import Settings
from rebar_service.store import Store
from rebar_service.v2_jobs import V2Job
from rebar_service.v2_worker import process_claimed_v2_job


def test_store_builds_stage_queue_and_only_solving_tracks_task_slots():
    store = Store(Settings(v2_queue_prefix="rebar:v2"))
    solving = store.v2_queue("solving")
    fitting = store.v2_queue("fitting")
    assert solving.ready_queue == "rebar:v2:solving:ready"
    assert fitting.ready_queue == "rebar:v2:fitting:ready"
    assert solving.track_task_slots is True
    assert fitting.track_task_slots is False


class FakeQueue:
    def __init__(self, acquire=True):
        self.acquire = acquire
        self.calls = []

    def acquire_task_slot(self, task_id, job_id, limit):
        self.calls.append(("slot", task_id, job_id, limit))
        return self.acquire

    def requeue_job(self, raw, job, delay=0):
        self.calls.append(("requeue", raw, job["job_id"]))

    def ack_job(self, raw, job, state="done"):
        self.calls.append(("ack", state, job["job_id"]))


class FakeStore:
    def __init__(self, queue):
        self.queue = queue
        self.nrow = {"attempt": 1, "state": "pending"}

    def get_v2_task(self, task_id):
        return {"task_id": task_id}

    def get_v2_n(self, task_id, n):
        return dict(self.nrow)


class FakeWorkflow:
    def __init__(self):
        self.jobs = []

    def dispatch(self, job):
        self.jobs.append(job)


def test_solver_worker_uses_per_task_solver_slot_before_dispatch():
    queue = FakeQueue(acquire=True)
    store = FakeStore(queue)
    workflow = FakeWorkflow()
    job = V2Job(stage="solving", kind="solve", task_id="t", n=4, attempt=1).to_dict()

    process_claimed_v2_job(
        store, workflow, "solving", "raw", job, "w",
        SimpleNamespace(max_concurrent_solvers_per_task=7), queue=queue,
    )

    assert queue.calls[0][0] == "slot"
    assert queue.calls[0][3] == 7
    assert len(workflow.jobs) == 1
    assert queue.calls[-1][0:2] == ("ack", "done")


def test_non_solver_worker_never_acquires_solver_slot():
    queue = FakeQueue(acquire=True)
    store = FakeStore(queue)
    workflow = FakeWorkflow()
    job = V2Job(stage="fitting", kind="fit", task_id="t", n=4, attempt=1).to_dict()

    process_claimed_v2_job(
        store, workflow, "fitting", "raw", job, "w",
        SimpleNamespace(max_concurrent_solvers_per_task=7), queue=queue,
    )

    assert not any(call[0] == "slot" for call in queue.calls)
    assert len(workflow.jobs) == 1


def test_stale_attempt_is_discarded_without_dispatch():
    queue = FakeQueue()
    store = FakeStore(queue)
    store.nrow = {"attempt": 2, "state": "pending"}
    workflow = FakeWorkflow()
    job = V2Job(stage="solving", kind="solve", task_id="t", n=4, attempt=1).to_dict()

    process_claimed_v2_job(
        store, workflow, "solving", "raw", job, "w",
        SimpleNamespace(max_concurrent_solvers_per_task=7), queue=queue,
    )

    assert workflow.jobs == []
    assert ("ack", "discarded", job["job_id"]) in queue.calls
