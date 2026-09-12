from __future__ import annotations

import logging
import os
import signal
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .store import Store
from .v2_jobs import V2Job, V2_STAGES

logger = logging.getLogger("rebar.v2_worker")


def _configure_worker_logging(settings: Any, stage: str, worker_id: str) -> None:
    level_name = str(getattr(settings, "worker_log_level", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        root.setLevel(level)
    logger.setLevel(level)

    log_dir = getattr(settings, "worker_log_dir", None)
    if not log_dir:
        return
    path = Path(str(log_dir)) / stage
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"{worker_id}.log"
    resolved = str(file_path.resolve())
    if any(isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == resolved
           for handler in logger.handlers):
        return
    handler = logging.FileHandler(file_path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.info("worker_file_log_enabled stage=%s worker_id=%s path=%s", stage, worker_id, file_path)


def _job_fields(job: V2Job) -> str:
    return (
        f"stage={job.stage} kind={job.kind} task_id={job.task_id} "
        f"n={job.n} attempt={job.attempt} request_id={job.request_id} job_id={job.job_id}"
    )


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
                    logger.warning("lease_heartbeat_failed worker_id=%s job_id=%s", self.worker_id, self.job.get("job_id"), exc_info=True)

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
        logger.exception("job_decode_failed stage=%s worker_id=%s", stage, worker_id)
        queue.ack_job(raw, job_data, "discarded")
        return
    if job.stage != stage:
        logger.warning("job_discarded reason=wrong_stage worker_id=%s expected=%s %s", worker_id, stage, _job_fields(job))
        queue.ack_job(raw, job_data, "discarded")
        return

    if job.kind in {"bars_request", "verification"}:
        kind = "bars" if job.kind == "bars_request" else "verification"
        if not job.request_id or store.get_v2_request(kind, job.request_id) is None:
            logger.warning("job_discarded reason=request_missing worker_id=%s %s", worker_id, _job_fields(job))
            queue.ack_job(raw, job_data, "discarded")
            return
    else:
        if store.get_v2_task(job.task_id) is None:
            logger.warning("job_discarded reason=task_missing worker_id=%s %s", worker_id, _job_fields(job))
            queue.ack_job(raw, job_data, "discarded")
            return
        if job.n is not None:
            nrow = store.get_v2_n(job.task_id, job.n)
            if nrow is None or int(nrow.get("attempt", 0)) != int(job.attempt) or nrow.get("state") == "cancelled":
                logger.info("job_discarded reason=stale_or_cancelled worker_id=%s %s", worker_id, _job_fields(job))
                queue.ack_job(raw, job_data, "discarded")
                return

    if stage == "solving":
        if not queue.acquire_task_slot(
            job.task_id, str(job.job_id), int(settings.max_concurrent_solvers_per_task)
        ):
            logger.debug("job_requeued reason=solver_slot_busy worker_id=%s %s", worker_id, _job_fields(job))
            queue.requeue_job(raw, job_data, delay=0.05)
            return

    started = time.monotonic()
    logger.info("job_started worker_id=%s %s", worker_id, _job_fields(job))
    try:
        workflow.dispatch(job)
    except Exception as exc:
        logger.exception("job_failed worker_id=%s elapsed_s=%.3f %s", worker_id, time.monotonic() - started, _job_fields(job))
        try:
            _mark_job_error(store, job, exc)
        except Exception:
            logger.exception("job_error_persist_failed worker_id=%s %s", worker_id, _job_fields(job))
            queue.requeue_job(raw, job_data, delay=1.0)
            return
        queue.ack_job(raw, job_data, "failed")
        logger.error("job_acked_failed worker_id=%s elapsed_s=%.3f %s", worker_id, time.monotonic() - started, _job_fields(job))
    else:
        queue.ack_job(raw, job_data, "done")
        logger.info("job_done worker_id=%s elapsed_s=%.3f %s", worker_id, time.monotonic() - started, _job_fields(job))


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
    _configure_worker_logging(settings, selected, worker_id)
    logger.info(
        "worker_started stage=%s worker_id=%s ready_queue=%s processing_queue=%s workload_queue=%s",
        selected, worker_id, queue.settings.ready_queue, queue.settings.processing_queue, queue.settings.workload_queue,
    )
    stopping = threading.Event()

    def stop(signum=None, *_args) -> None:
        logger.info("worker_stop_signal stage=%s worker_id=%s signal=%s", selected, worker_id, signum)
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_reaper = 0.0

    while not stopping.is_set():
        now = time.monotonic()
        if now - last_reaper >= max(15.0, settings.job_lease_seconds / 2.0):
            try:
                recovered = queue.requeue_stale_jobs()
                if recovered:
                    logger.warning("stale_jobs_requeued stage=%s worker_id=%s count=%s", selected, worker_id, recovered)
            except Exception:
                logger.exception("queue_reaper_failed stage=%s worker_id=%s", selected, worker_id)
            last_reaper = now
        claimed = queue.claim_job(worker_id, settings.worker_claim_timeout_seconds)
        if claimed is None:
            continue
        raw, job_data = claimed
        with V2LeaseHeartbeat(queue, job_data, worker_id):
            process_claimed_v2_job(
                store, workflow, selected, raw, job_data, worker_id, settings, queue=queue
            )

    logger.info("worker_stopped stage=%s worker_id=%s", selected, worker_id)


def main() -> None:
    run_v2_worker()


if __name__ == "__main__":
    main()
