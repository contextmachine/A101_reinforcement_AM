"""Entrypoint of the alternative-solver worker (``python -m rebar_service.alt_worker``).

Consumes the *main* v2 job queue of its configuration (``REBAR_READY_QUEUE`` etc.), so a
ConfigMap that points those names at ``rebar:<env>:solver:*`` makes this worker the solver of
every task the API enqueues there; pointing the API back at ``rebar:<env>:jobs:*`` rolls back to
the production pipeline. Runs as a KEDA ``ScaledJob``: with ``REBAR_WORKER_EXIT_WHEN_IDLE=true``
the process drains its queue and exits.
"""

from __future__ import annotations

# ortools bundles its own (older) libhighs under the same soname as highspy; whichever loads first
# wins the symbol table, so this process loads CP-SAT first and must never import highspy.
from ortools.sat.python import cp_model  # noqa: F401  (must stay the first import)

from .config import get_settings
from .redis_queue import QueueNames, RedisQueue
from .store import Store
from .worker_loop import run_queue_worker


def run_alt_worker() -> None:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    store = Store(settings)
    queue = RedisQueue(settings, names=QueueNames.main(settings))
    from .altsolver.pipeline import AltSolverPipeline

    pipeline = AltSolverPipeline(store, settings)

    def handle(job_data: dict, worker_id: str) -> None:
        pipeline.dispatch(job_data, worker_id)

    run_queue_worker(
        settings=settings, queue=queue, handle=handle, worker_name="alt-solver",
        exit_when_idle=settings.worker_exit_when_idle,
    )


def main() -> None:
    run_alt_worker()


if __name__ == "__main__":
    main()
