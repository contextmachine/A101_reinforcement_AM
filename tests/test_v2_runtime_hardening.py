from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace

from rebar_service.config import Settings
from rebar_service.postgres_store import PostgresStore
from rebar_service.v2_jobs import V2Job
from rebar_service.v2_worker import process_claimed_v2_job


class _Result:
    rowcount = 1


class _CaptureConnection:
    def __init__(self, database):
        self.database = database

    def execute(self, statement, params=None):
        self.database.calls.append((str(statement), dict(params or {})))
        return _Result()


class _CaptureDatabase:
    def __init__(self):
        self.calls = []

    @contextmanager
    def begin(self):
        yield _CaptureConnection(self)


def test_v2_state_updates_use_typed_boolean_guards_instead_of_reusing_nullable_binds():
    db = _CaptureDatabase()
    store = PostgresStore(Settings(), database=db)

    store.set_v2_preparation_state("task", "preparing")
    prep_sql, prep_params = db.calls[0]
    n_sql, n_params = db.calls[1]
    assert "CASE WHEN :is_success" in prep_sql
    assert "CASE WHEN :state='success'" not in prep_sql
    assert prep_params["is_success"] is False
    assert "CASE WHEN :is_error" in n_sql
    assert "CASE WHEN :n_state='error'" not in n_sql
    assert n_params["is_error"] is False

    db.calls.clear()
    store.set_v2_n_state("task", 4, "solving", attempt=1)
    sql, params = db.calls[0]
    assert "CAST(:state AS varchar)" in sql
    assert "WHEN :has_result" in sql and "WHEN :has_error" in sql
    assert ":result IS NULL" not in sql and ":error IS NULL" not in sql
    assert params["has_result"] is False and params["has_error"] is False

    db.calls.clear()
    store.set_v2_request_state("bars", "request", "baring")
    sql, params = db.calls[0]
    assert "CAST(:state AS varchar)" in sql
    assert "WHEN :has_result" in sql and "WHEN :has_error" in sql
    assert params["has_result"] is False and params["has_error"] is False


class _Queue:
    def __init__(self):
        self.calls = []

    def acquire_task_slot(self, *_args):
        return True

    def requeue_job(self, raw, job, delay=0):
        self.calls.append(("requeue", raw, job["job_id"], delay))

    def ack_job(self, raw, job, state="done"):
        self.calls.append(("ack", state, job["job_id"]))


class _Store:
    def __init__(self, *, fail_mark_error=False):
        self.fail_mark_error = fail_mark_error

    def get_v2_task(self, task_id):
        return {"task_id": task_id}

    def get_v2_n(self, task_id, n):
        return {"attempt": 1, "state": "pending"}

    def set_v2_n_state(self, *args, **kwargs):
        if self.fail_mark_error:
            raise RuntimeError("cannot persist error")
        return True


class _Workflow:
    def __init__(self, exc=None):
        self.exc = exc

    def dispatch(self, job):
        if self.exc is not None:
            raise self.exc


def test_worker_logs_successful_job_lifecycle(caplog):
    caplog.set_level(logging.INFO, logger="rebar.v2_worker")
    queue = _Queue()
    job = V2Job(stage="fitting", kind="fit", task_id="task", n=4, attempt=1).to_dict()

    process_claimed_v2_job(
        _Store(), _Workflow(), "fitting", "raw", job, "worker-1",
        SimpleNamespace(max_concurrent_solvers_per_task=1), queue=queue,
    )

    messages = [record.getMessage() for record in caplog.records]
    assert any("job_started" in message and "stage=fitting" in message for message in messages)
    assert any("job_done" in message and "stage=fitting" in message for message in messages)
    assert ("ack", "done", job["job_id"]) in queue.calls


def test_worker_requeues_if_error_state_itself_cannot_be_persisted(caplog):
    caplog.set_level(logging.ERROR, logger="rebar.v2_worker")
    queue = _Queue()
    job = V2Job(stage="solving", kind="solve", task_id="task", n=4, attempt=1).to_dict()

    process_claimed_v2_job(
        _Store(fail_mark_error=True), _Workflow(RuntimeError("solver exploded")),
        "solving", "raw", job, "worker-1",
        SimpleNamespace(max_concurrent_solvers_per_task=1), queue=queue,
    )

    assert any(call[0] == "requeue" for call in queue.calls)
    assert not any(call[:2] == ("ack", "failed") for call in queue.calls)
    assert any("job_error_persist_failed" in record.getMessage() for record in caplog.records)
