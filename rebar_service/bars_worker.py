"""Entrypoint of the isolated ``/v2/bars`` worker (queue ``rebar:bars:*``).

Runs as a KEDA ``ScaledJob``: with ``REBAR_WORKER_EXIT_WHEN_IDLE=true`` the process
drains its queue and exits, which completes the Job.
"""

from __future__ import annotations

import traceback

from .config import get_settings
from .redis_queue import QueueNames, RedisQueue
from .store import Store
from .worker_loop import run_queue_worker


def run_bars_worker() -> None:
    settings = get_settings()
    store = Store(settings)
    queue = RedisQueue(settings, names=QueueNames.bars(settings))

    def handle(job_data: dict, worker_id: str) -> None:
        try:
            from .v2.workers import handle_bars_job
        except ImportError:
            # The v2 pipeline package is not installed in this image yet.
            traceback.print_exc()
            raise
        handle_bars_job(store, job_data, worker_id)

    run_queue_worker(
        settings=settings,
        queue=queue,
        handle=handle,
        worker_name="bars-worker",
        exit_when_idle=settings.worker_exit_when_idle,
    )


def main() -> None:
    run_bars_worker()


if __name__ == "__main__":
    main()
