"""`run_queue_worker` contract tests. No Redis: the queue is a plain stub object."""

from __future__ import annotations

import threading

import pytest

from rebar_service.config import Settings
from rebar_service.worker_loop import LeaseHeartbeat, run_queue_worker


class StubQueue:
    """Minimal RedisQueue surface used by the worker loop."""

    def __init__(self, jobs, settings: Settings, claim_error: Exception | None = None):
        self.settings = settings
        self._jobs = list(jobs)
        self.claim_error = claim_error
        self.claims: list[str] = []
        self.acks: list[tuple[str, str]] = []
        self.heartbeats: list[tuple[str, str]] = []
        self.reaped = 0
        self.ack_error: Exception | None = None

    def claim_job(self, worker_id, timeout):
        self.claims.append(worker_id)
        if self.claim_error is not None:
            raise self.claim_error
        if not self._jobs:
            return None
        job = self._jobs.pop(0)
        return f"raw:{job['job_id']}", job

    def heartbeat_job(self, job, worker_id):
        self.heartbeats.append((str(job["job_id"]), worker_id))

    def ack_job(self, raw, job, state="done"):
        if self.ack_error is not None:
            raise self.ack_error
        self.acks.append((str(job["job_id"]), state))

    def requeue_stale_jobs(self, grace_seconds=10.0):
        self.reaped += 1
        return 0


def _job(job_id: str, kind: str = "bars") -> dict:
    return {"kind": kind, "task_id": "t1", "job_id": job_id, "payload": {}, "generation": 0}


def _settings() -> Settings:
    return Settings(worker_claim_timeout_seconds=1, job_lease_seconds=90)


def test_exit_when_idle_drains_the_queue_and_returns():
    settings = _settings()
    queue = StubQueue([_job("a"), _job("b")], settings)
    seen: list[tuple[str, str]] = []

    run_queue_worker(
        settings=settings,
        queue=queue,
        handle=lambda job, worker_id: seen.append((str(job["job_id"]), worker_id)),
        worker_name="bars-worker",
        exit_when_idle=True,
    )

    assert [job_id for job_id, _ in seen] == ["a", "b"]
    assert queue.acks == [("a", "done"), ("b", "done")]
    assert queue.reaped == 1
    # One worker id for the whole process, stable across jobs; HOSTNAME wins in Kubernetes.
    import os

    worker_ids = {worker_id for _, worker_id in seen}
    assert len(worker_ids) == 1
    assert worker_ids.pop().startswith(f"{os.getenv('HOSTNAME', 'bars-worker')}-")


def test_handler_exception_acks_failed_and_keeps_the_loop_running():
    settings = _settings()
    queue = StubQueue([_job("a"), _job("b")], settings)
    handled: list[str] = []

    def handle(job, worker_id):
        handled.append(str(job["job_id"]))
        if job["job_id"] == "a":
            raise RuntimeError("boom")

    run_queue_worker(
        settings=settings,
        queue=queue,
        handle=handle,
        worker_name="bars-worker",
        exit_when_idle=True,
    )

    assert handled == ["a", "b"]
    assert queue.acks == [("a", "failed"), ("b", "done")]


def test_import_error_in_the_handler_is_reported_as_a_failed_job():
    settings = _settings()
    queue = StubQueue([_job("a")], settings)

    def handle(job, worker_id):
        raise ImportError("rebar_service.v2.workers is not available")

    run_queue_worker(
        settings=settings,
        queue=queue,
        handle=handle,
        worker_name="bars-worker",
        exit_when_idle=True,
    )

    assert queue.acks == [("a", "failed")]


def test_stopping_event_ends_a_long_running_worker_without_exit_when_idle():
    settings = _settings()
    queue = StubQueue([_job("a")], settings)
    stopping = threading.Event()

    def handle(job, worker_id):
        stopping.set()

    run_queue_worker(
        settings=settings,
        queue=queue,
        handle=handle,
        worker_name="worker",
        exit_when_idle=False,
        stopping=stopping,
    )

    assert queue.acks == [("a", "done")]
    assert len(queue.claims) == 1


def test_claim_failure_fails_the_exit_when_idle_job_instead_of_reporting_success():
    settings = _settings()
    queue = StubQueue([], settings, claim_error=ConnectionError("redis down"))

    with pytest.raises(ConnectionError):
        run_queue_worker(
            settings=settings,
            queue=queue,
            handle=lambda job, worker_id: None,
            worker_name="bars-worker",
            exit_when_idle=True,
        )

    assert queue.acks == []


def test_claim_failure_keeps_a_long_running_worker_alive(monkeypatch):
    settings = _settings()
    queue = StubQueue([], settings, claim_error=ConnectionError("redis down"))
    stopping = threading.Event()
    attempts = {"count": 0}
    original = queue.claim_job

    def flaky(worker_id, timeout):
        attempts["count"] += 1
        if attempts["count"] >= 3:
            stopping.set()
        return original(worker_id, timeout)

    queue.claim_job = flaky
    monkeypatch.setattr("rebar_service.worker_loop.time.sleep", lambda _seconds: None)
    run_queue_worker(
        settings=settings,
        queue=queue,
        handle=lambda job, worker_id: None,
        worker_name="bars-worker",
        exit_when_idle=False,
        stopping=stopping,
    )
    assert attempts["count"] >= 3 and queue.acks == []


def test_ack_failure_is_swallowed_so_the_reaper_can_recover_the_job():
    settings = _settings()
    queue = StubQueue([_job("a")], settings)
    queue.ack_error = ConnectionError("redis down")

    run_queue_worker(
        settings=settings,
        queue=queue,
        handle=lambda job, worker_id: None,
        worker_name="bars-worker",
        exit_when_idle=True,
    )

    assert queue.acks == []


def test_handler_receives_a_copy_of_the_job_dict():
    settings = _settings()
    job = _job("a")
    queue = StubQueue([job], settings)

    def handle(job_data, worker_id):
        job_data["kind"] = "mutated"

    run_queue_worker(
        settings=settings,
        queue=queue,
        handle=handle,
        worker_name="bars-worker",
        exit_when_idle=True,
    )

    assert job["kind"] == "bars"


def test_lease_heartbeat_renews_the_lease_while_the_handler_runs():
    settings = Settings(job_lease_seconds=3)
    queue = StubQueue([], settings)
    job = _job("a")
    done = threading.Event()

    with LeaseHeartbeat(queue, job, "worker-1"):
        # interval = max(1.0, 3/3) = 1.0s
        done.wait(1.4)

    assert queue.heartbeats == [("a", "worker-1")]


def test_lease_heartbeat_ignores_transient_redis_failures():
    settings = Settings(job_lease_seconds=3)

    class Flaky(StubQueue):
        def heartbeat_job(self, job, worker_id):
            super().heartbeat_job(job, worker_id)
            raise ConnectionError("redis down")

    queue = Flaky([], settings)
    with LeaseHeartbeat(queue, _job("a"), "worker-1"):
        threading.Event().wait(1.4)

    assert queue.heartbeats == [("a", "worker-1")]


@pytest.mark.parametrize(
    "module_name, entrypoint",
    [
        ("rebar_service.bars_worker", "run_bars_worker"),
        ("rebar_service.verification_worker", "run_verification_worker"),
    ],
)
def test_isolated_worker_entrypoints_import_without_redis_or_postgres(module_name, entrypoint):
    import importlib

    module = importlib.import_module(module_name)
    assert callable(getattr(module, entrypoint))
    assert callable(module.main)


def test_main_worker_dispatches_v2_jobs_and_no_longer_reads_meta_job_limits():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "rebar_service/worker.py").read_text(encoding="utf-8")
    assert 'str(job_data.get("kind", "")).startswith("v2_")' in source
    assert "from .v2.pipeline import V2Pipeline" in source
    assert "except ImportError" in source
    assert "v2_workflow.dispatch(job_data, worker_id)" in source
    assert "task_limit = int(settings.max_jobs_per_task)" in source
    assert 'meta.get("max_concurrent_jobs")' not in source
    # The SIGTERM handler of the long-lived Deployment worker stays.
    assert "signal.signal(signal.SIGTERM" in source
