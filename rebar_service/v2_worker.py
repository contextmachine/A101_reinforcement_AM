from __future__ import annotations

import os
import signal
import threading
import time
import traceback
import uuid
from typing import Any

from .config import Settings, get_settings
from .store import Store
from .v2_jobs import V2Job, V2_STAGES


class V2LeaseHeartbeat:
    def __init__(self, queue: Any, job: dict[str, Any], worker_id: str) -> None:
        self.queue = queue
        self.job = job
        self.worker_id = worker_id
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        interval = max(1.0, float(self.queue.settings.job_lease_seconds) / 3.0)

        def run() -> None:
            while not self.stop.wait(interval):
                try:
                    self.queue.heartbeat_job(self.job, self.worker_id)
                except Exception:
                    pass

        self.thread = threading.Thread(target=run, name="rebar-v2-lease-heartbeat", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)


def _mark_job_error(store: Any, job: V2Job, exc: Exception) -> None:
    error = {
        "stage": job.stage,
        "job_id": job.job_id,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }
    if job.kind == "bars_request" and job.request_id:
        store.set_v2_request_state("bars", job.request_id, "error", error=error)
    elif job.kind == "verification" and job.request_id:
        store.set_v2_request_state("verification", job.request_id, "error", error=error)
    elif job.n is not None:
        store.set_v2_n_state(job.task_id, job.n, "error", attempt=job.attempt, error=error)
    else:
        store.set_v2_preparation_state(job.task_id, "error", error=error)


def process_claimed_v2_job(
    store: Any,
    workflow: Any,
    worker_stage: str,
    raw: str,
    job_data: dict[str, Any],
    worker_id: str,
    settings: Any,
    *,
    queue: Any | None = None,
) -> None:
    stage = str(worker_stage).lower()
    queue = queue or store.v2_queue(stage)
    try:
        job = V2Job.from_value(job_data)
    except Exception:
        queue.ack_job(raw, job_data, "discarded")
        return
    if job.stage != stage:
        queue.ack_job(raw, job_data, "discarded")
        return

    if job.kind in {"bars_request", "verification"}:
        kind = "bars" if job.kind == "bars_request" else "verification"
        if not job.request_id or store.get_v2_request(kind, job.request_id) is None:
            queue.ack_job(raw, job_data, "discarded")
            return
    else:
        if store.get_v2_task(job.task_id) is None:
            queue.ack_job(raw, job_data, "discarded")
            return
        if job.n is not None:
            nrow = store.get_v2_n(job.task_id, job.n)
            if nrow is None or int(nrow.get("attempt", 0)) != int(job.attempt) or nrow.get("state") == "cancelled":
                queue.ack_job(raw, job_data, "discarded")
                return

    if stage == "solving":
        if not queue.acquire_task_slot(
            job.task_id, str(job.job_id), int(settings.max_concurrent_solvers_per_task)
        ):
            queue.requeue_job(raw, job_data, delay=0.05)
            return

    try:
        workflow.dispatch(job)
    except Exception as exc:
        _mark_job_error(store, job, exc)
        queue.ack_job(raw, job_data, "failed")
    else:
        queue.ack_job(raw, job_data, "done")


def run_v2_worker(stage: str | None = None) -> None:
    settings = get_settings()
    selected = str(stage or settings.v2_worker_stage or "").lower()
    if selected not in V2_STAGES:
        raise ValueError("REBAR_V2_WORKER_STAGE must be preparing|solving|fitting|baring|validation")

    from .v2_pipeline import V2Pipeline

    store = Store(settings)
    queue = store.v2_queue(selected)
    workflow = V2Pipeline(store, settings)
    worker_id = f"{os.getenv('HOSTNAME', 'v2-worker')}-{selected}-{uuid.uuid4().hex[:8]}"
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
                queue.requeue_stale_jobs()
            except Exception:
                traceback.print_exc()
            last_reaper = now
        claimed = queue.claim_job(worker_id, settings.worker_claim_timeout_seconds)
        if claimed is None:
            continue
        raw, job_data = claimed
        with V2LeaseHeartbeat(queue, job_data, worker_id):
            process_claimed_v2_job(
                store, workflow, selected, raw, job_data, worker_id, settings, queue=queue
            )


def main() -> None:
    run_v2_worker()


if __name__ == "__main__":
    main()
