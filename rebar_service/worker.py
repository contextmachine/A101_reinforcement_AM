from __future__ import annotations

import os
import signal
import threading
import time
import traceback
import uuid

from .config import get_settings
from .pipeline import PipelineJob, PipelineWorkflow
from .store import RedisStore


class LeaseHeartbeat:
    def __init__(self, store: RedisStore, job: dict, worker_id: str) -> None:
        self.store = store
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


def run_worker() -> None:
    settings = get_settings()
    store = RedisStore(settings)
    workflow = PipelineWorkflow(store, settings)
    worker_id = f"{os.getenv('HOSTNAME', 'worker')}-{uuid.uuid4().hex[:8]}"
    stopping = threading.Event()

    def stop(*_args) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_reaper = 0.0
    while not stopping.is_set():
        now = time.monotonic()
        if now - last_reaper >= max(15.0, settings.job_lease_seconds / 2.0):
            try:
                store.requeue_stale_jobs()
            except Exception:
                traceback.print_exc()
            last_reaper = now

        claimed = store.claim_job(worker_id, settings.worker_claim_timeout_seconds)
        if claimed is None:
            continue
        raw, job_data = claimed
        task_id = str(job_data.get("task_id", ""))
        job_id = str(job_data.get("job_id", ""))

        meta = store.get_meta(task_id)
        if meta is None:
            store.ack_job(raw, job_data, "discarded")
            continue
        if meta.get("cancelled"):
            store.ack_job(raw, job_data, "cancelled")
            continue
        if int(job_data.get("generation", 0)) != store.generation(task_id):
            store.ack_job(raw, job_data, "discarded")
            continue
        if meta.get("paused"):
            store.requeue_job(raw, job_data, delay=1.0)
            continue

        task_limit = int(meta.get("max_concurrent_jobs") or settings.max_jobs_per_task)
        if not store.acquire_task_slot(task_id, job_id, task_limit):
            store.requeue_job(raw, job_data, delay=0.05)
            continue

        try:
            with LeaseHeartbeat(store, job_data, worker_id):
                workflow.dispatch(PipelineJob.from_value(job_data))
        except Exception as exc:
            traceback.print_exc()
            try:
                store.publish_event(
                    task_id,
                    "job_failed",
                    {
                        "job_id": job_id,
                        "kind": job_data.get("kind"),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            finally:
                store.ack_job(raw, job_data, "failed")
                store.refresh_pipeline_state(task_id)
        else:
            store.ack_job(raw, job_data, "done")
            store.refresh_pipeline_state(task_id)


def main() -> None:
    run_worker()


if __name__ == "__main__":
    main()
