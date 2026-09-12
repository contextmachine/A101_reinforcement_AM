"""Generic Redis-queue worker loop shared by every worker entrypoint.

`rebar_service.worker` keeps its own (task-aware) loop and only reuses `LeaseHeartbeat`;
the isolated `/v2/bars` and `/v2/verification` workers run `run_queue_worker`, which is
queue-agnostic: it claims, heartbeats, dispatches and acks, and — with
``exit_when_idle=True`` — returns as soon as its queue is empty so that a KEDA
``ScaledJob`` Pod can finish.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Mapping

from .config import Settings
from .redis_queue import RedisQueue


class LeaseHeartbeat:
    """Keeps a claimed job's Redis lease alive while the handler runs.

    ``client`` is anything exposing ``settings`` and ``heartbeat_job`` — both the
    `Store` facade and a bare `RedisQueue` qualify.
    """

    def __init__(self, client: Any, job: Mapping[str, Any], worker_id: str) -> None:
        self.store = client
        self.job = job
        self.worker_id = worker_id
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        interval = max(1.0, self.store.settings.job_lease_seconds / 3.0)

        def run() -> None:
            while not self.stop.wait(interval):
                try:
                    self.store.heartbeat_job(self.job, self.worker_id)
                except Exception:
                    # A temporary heartbeat failure must not abort a running solver.
                    pass

        self.thread = threading.Thread(target=run, name="rebar-lease-heartbeat", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)


def make_worker_id(worker_name: str) -> str:
    return f"{os.getenv('HOSTNAME', worker_name)}-{uuid.uuid4().hex[:8]}"


def _install_signal_handlers(stopping: threading.Event) -> None:
    def stop(*_args) -> None:
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, stop)
        except ValueError:
            # Not the main thread (tests, embedded use): the caller owns shutdown.
            pass


def run_queue_worker(
    *,
    settings: Settings,
    queue: RedisQueue,
    handle: Callable[[dict, str], None],
    worker_name: str,
    exit_when_idle: bool,
    stopping: threading.Event | None = None,
) -> None:
    """Claim → heartbeat → ``handle(job, worker_id)`` → ack, until stopped or idle.

    A handler exception is logged with its traceback and the job is acked ``failed``;
    persisting the durable error state is the handler's responsibility.
    """

    worker_id = make_worker_id(worker_name)
    if stopping is None:
        stopping = threading.Event()
        _install_signal_handlers(stopping)

    reaper_interval = max(15.0, settings.job_lease_seconds / 2.0)
    last_reaper = 0.0
    while not stopping.is_set():
        now = time.monotonic()
        if now - last_reaper >= reaper_interval:
            try:
                queue.requeue_stale_jobs()
            except Exception:
                traceback.print_exc()
            last_reaper = now

        try:
            claimed = queue.claim_job(worker_id, settings.worker_claim_timeout_seconds)
        except Exception:
            traceback.print_exc()
            if exit_when_idle:
                # A KEDA Job must fail loudly instead of reporting an idle success.
                raise
            time.sleep(1.0)
            continue
        if claimed is None:
            if exit_when_idle:
                return
            continue

        raw, job_data = claimed
        try:
            with LeaseHeartbeat(queue, job_data, worker_id):
                handle(dict(job_data), worker_id)
        except Exception:
            traceback.print_exc()
            _ack(queue, raw, job_data, "failed")
        else:
            _ack(queue, raw, job_data, "done")


def _ack(queue: RedisQueue, raw: str, job_data: Mapping[str, Any], state: str) -> None:
    try:
        queue.ack_job(raw, job_data, state)
    except Exception:
        # The job stays in the processing list; the reaper returns it to ready.
        traceback.print_exc()
