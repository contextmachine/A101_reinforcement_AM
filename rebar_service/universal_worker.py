from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from typing import Optional

from .component_store import ComponentStore
from .component_workflow import ComponentWorkflow, WorkflowConfig


_STOP = threading.Event()


def redis_client():
    import redis
    url = os.getenv("REBAR_REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    return redis.Redis.from_url(url, decode_responses=False)


def run_component_loop() -> None:
    store = ComponentStore(redis_client())
    workflow = ComponentWorkflow(store, WorkflowConfig())
    store.recover_expired()
    last_recovery = 0.0
    while not _STOP.is_set():
        if time.time() - last_recovery >= 30:
            store.recover_expired()
            last_recovery = time.time()
        job = store.claim(timeout=2)
        if job is None:
            continue
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(max(5, store.lease_seconds // 3)):
                try:
                    store.touch_lease(job)
                except Exception:
                    pass
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            if int(job.generation) == store.generation(job.task_id):
                workflow.dispatch(job)
            store.ack(job)
        except BaseException as exc:
            error = "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc())
            print("component job failed", job.job_id, error, flush=True)
            store.fail(job, error)
        finally:
            heartbeat_stop.set()
            thread.join(timeout=1)


def start_legacy_worker() -> Optional[subprocess.Popen]:
    if os.getenv("REBAR_RUN_LEGACY_WORKER", "true").lower() in {"0", "false", "no"}:
        return None
    command = os.getenv("REBAR_LEGACY_WORKER_COMMAND", "%s -m rebar_service.worker" % sys.executable)
    return subprocess.Popen(command, shell=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Universal legacy + component worker")
    parser.add_argument("--component-only", action="store_true")
    args = parser.parse_args()

    child = None if args.component_only else start_legacy_worker()

    def stop(*_):
        _STOP.set()
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        run_component_loop()
    finally:
        stop()
        if child:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
