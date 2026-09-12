from __future__ import annotations

from typing import Any, Mapping

from .config import Settings
from .postgres_store import PostgresStore
from .redis_queue import QueueNames, RedisQueue
from .v2.store import V2Store


class Store(PostgresStore):
    """Application storage facade: PostgreSQL durable state + Redis queue state."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.queue = RedisQueue(settings)
        # Isolated queues of the /v2/bars and /v2/verification workers (own KEDA ScaledJobs).
        self.bars_queue = RedisQueue(settings, names=QueueNames.bars(settings))
        self.verification_queue = RedisQueue(settings, names=QueueNames.verification(settings))
        # /v2 tables (v2_tasks, v2_task_ns, v2_artifacts, v2_bar_tasks, v2_verification_tasks).
        backend = None
        if settings.artifact_path is not None:
            from .v2.artifacts import FileArtifactBackend

            backend = FileArtifactBackend(
                settings.artifact_path, settings.artifact_cache_path,
                cache_ttl_seconds=float(settings.artifact_cache_ttl_seconds),
            )
        self.v2 = V2Store(self.database, artifact_backend=backend)

    @property
    def redis(self):
        """Queue Redis client kept only for diagnostics/backward-compatible tooling."""
        return self.queue.redis

    def ping(self) -> bool:
        return bool(super().ping() and self.queue.ping())

    @staticmethod
    def task_key(task_id: str, suffix: str) -> str:
        return RedisQueue.task_key(task_id, suffix)

    def lock(self, name: str, timeout: float = 30.0):
        return self.queue.lock(name, timeout)

    def enqueue_pipeline_job(self, job: Mapping[str, Any]) -> bool:
        return self.queue.enqueue_pipeline_job(job)

    def claim_job(self, worker_id: str, timeout: int):
        return self.queue.claim_job(worker_id, timeout)

    def heartbeat_job(self, job: Mapping[str, Any], worker_id: str) -> None:
        self.queue.heartbeat_job(job, worker_id)

    def ack_job(self, raw: str, job: Mapping[str, Any], state: str = "done") -> None:
        self.queue.ack_job(raw, job, state)

    def requeue_job(self, raw: str, job: Mapping[str, Any], delay: float = 0.0) -> None:
        self.queue.requeue_job(raw, job, delay)

    def requeue_stale_jobs(self, grace_seconds: float = 10.0) -> int:
        return self.queue.requeue_stale_jobs(grace_seconds)

    def acquire_task_slot(self, task_id: str, job_id: str, limit: int) -> bool:
        return self.queue.acquire_task_slot(task_id, job_id, limit)

    def pending_jobs(self, task_id: str) -> int:
        return self.queue.pending_jobs(task_id)


# Temporary import compatibility for third-party code/tests; durable state is no longer in Redis.
RedisStore = Store
