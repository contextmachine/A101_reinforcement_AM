from __future__ import annotations

import contextlib
import time
import uuid
from typing import Any, Iterator, Mapping

from .codec import sha256
from .config import Settings
from .jsonutil import dumps, loads


class RedisQueue:
    """Redis is intentionally limited to queue and worker-coordination state."""

    ENQUEUE_LUA = """
    local dedupe = KEYS[1]
    local ready = KEYS[2]
    local workload = KEYS[3]
    local pending = KEYS[4]
    if redis.call('SETNX', dedupe, ARGV[1]) == 0 then return 0 end
    redis.call('EXPIRE', dedupe, ARGV[2])
    redis.call('LPUSH', ready, ARGV[3])
    redis.call('LPUSH', workload, ARGV[1])
    redis.call('SADD', pending, ARGV[1])
    redis.call('EXPIRE', pending, ARGV[2])
    return 1
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._redis = None

    @property
    def redis(self):
        if self._redis is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - installed in production image
                raise ImportError("Установите пакет redis") from exc
            self._redis = redis.Redis.from_url(self.settings.redis_url, decode_responses=False)
        return self._redis

    def ping(self) -> bool:
        return bool(self.redis.ping())

    @staticmethod
    def task_key(task_id: str, suffix: str) -> str:
        return f"rebar:task:{task_id}:{suffix}"

    @staticmethod
    def job_key(job_id: str) -> str:
        return f"rebar:job:{job_id}"

    @staticmethod
    def dedupe_key(value: str) -> str:
        return f"rebar:job-dedupe:{sha256(value.encode())}"

    @contextlib.contextmanager
    def lock(self, name: str, timeout: float = 30.0) -> Iterator[None]:
        key = f"rebar:lock:{name}"
        token = uuid.uuid4().hex.encode()
        deadline = time.monotonic() + timeout
        while not self.redis.set(key, token, nx=True, px=max(1000, int(timeout * 2000))):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Не удалось получить Redis lock {name}")
            time.sleep(0.02)
        try:
            yield
        finally:
            script = """
            if redis.call('GET', KEYS[1]) == ARGV[1] then
              return redis.call('DEL', KEYS[1])
            end
            return 0
            """
            try:
                self.redis.eval(script, 1, key, token)
            except Exception:
                pass

    def enqueue_pipeline_job(self, job: Mapping[str, Any]) -> bool:
        row = dict(job)
        raw = dumps(row)
        job_id = str(row["job_id"])
        task_id = str(row["task_id"])
        dedupe = self.dedupe_key(str(row["dedupe_key"]))
        pending = self.task_key(task_id, "pending")
        ttl = int(self.settings.queue_state_ttl_seconds)
        try:
            queued = bool(
                self.redis.eval(
                    self.ENQUEUE_LUA,
                    4,
                    dedupe,
                    self.settings.ready_queue,
                    self.settings.workload_queue,
                    pending,
                    job_id,
                    ttl,
                    raw,
                )
            )
        except Exception:
            if not self.redis.set(dedupe, job_id, nx=True, ex=ttl):
                return False
            pipe = self.redis.pipeline(transaction=True)
            pipe.lpush(self.settings.ready_queue, raw)
            pipe.lpush(self.settings.workload_queue, job_id)
            pipe.sadd(pending, job_id)
            pipe.expire(pending, ttl)
            pipe.execute()
            queued = True
        if queued:
            self.redis.set(self.job_key(job_id), dumps({**row, "state": "pending"}), ex=ttl)
        return queued

    def claim_job(self, worker_id: str, timeout: int) -> tuple[str, dict[str, Any]] | None:
        raw = self.redis.brpoplpush(self.settings.ready_queue, self.settings.processing_queue, timeout=int(timeout))
        if raw is None:
            return None
        text_value = raw.decode() if isinstance(raw, bytes) else str(raw)
        job = loads(text_value, None)
        if not isinstance(job, dict):
            self.redis.lrem(self.settings.processing_queue, 1, raw)
            return None
        job_id = str(job["job_id"])
        lease = self.job_key(job_id) + ":lease"
        self.redis.set(lease, worker_id, ex=self.settings.job_lease_seconds)
        state = {**job, "state": "claimed", "worker_id": worker_id, "claimed_at": time.time()}
        self.redis.set(self.job_key(job_id), dumps(state), ex=self.settings.queue_state_ttl_seconds)
        return text_value, job

    def heartbeat_job(self, job: Mapping[str, Any], worker_id: str) -> None:
        job_id = str(job["job_id"])
        task_id = str(job["task_id"])
        self.redis.set(self.job_key(job_id) + ":lease", worker_id, ex=self.settings.job_lease_seconds)
        self.redis.zadd(self.task_key(task_id, "slots"), {job_id: time.time() + self.settings.job_lease_seconds})
        self.redis.expire(self.task_key(task_id, "slots"), self.settings.queue_state_ttl_seconds)

    def ack_job(self, raw: str, job: Mapping[str, Any], state: str = "done") -> None:
        task_id = str(job["task_id"])
        job_id = str(job["job_id"])
        self.redis.lrem(self.settings.processing_queue, 1, raw)
        self.redis.lrem(self.settings.workload_queue, 1, job_id)
        self.redis.delete(self.job_key(job_id) + ":lease")
        self.redis.zrem(self.task_key(task_id, "slots"), job_id)
        self.redis.srem(self.task_key(task_id, "pending"), job_id)
        if job.get("dedupe_key"):
            self.redis.delete(self.dedupe_key(str(job["dedupe_key"])))
        self.redis.set(
            self.job_key(job_id),
            dumps({**job, "state": state, "finished_at": time.time()}),
            ex=self.settings.queue_state_ttl_seconds,
        )

    def requeue_job(self, raw: str, job: Mapping[str, Any], delay: float = 0.0) -> None:
        task_id = str(job["task_id"])
        job_id = str(job["job_id"])
        self.redis.lrem(self.settings.processing_queue, 1, raw)
        self.redis.delete(self.job_key(job_id) + ":lease")
        self.redis.zrem(self.task_key(task_id, "slots"), job_id)
        self.redis.set(
            self.job_key(job_id),
            dumps({**job, "state": "pending"}),
            ex=self.settings.queue_state_ttl_seconds,
        )
        if delay:
            time.sleep(delay)
        self.redis.lpush(self.settings.ready_queue, raw)

    def requeue_stale_jobs(self, grace_seconds: float = 10.0) -> int:
        recovered = 0
        with self.lock("queue-reaper", timeout=2.0):
            now = time.time()
            for raw in self.redis.lrange(self.settings.processing_queue, 0, -1):
                text_value = raw.decode() if isinstance(raw, bytes) else str(raw)
                job = loads(text_value, None)
                if not isinstance(job, dict):
                    self.redis.lrem(self.settings.processing_queue, 1, raw)
                    continue
                job_id = str(job["job_id"])
                if self.redis.exists(self.job_key(job_id) + ":lease"):
                    continue
                state = loads(self.redis.get(self.job_key(job_id)), {})
                claimed_at = float(state.get("claimed_at", job.get("created_at", 0)))
                if now - claimed_at < self.settings.job_lease_seconds + grace_seconds:
                    continue
                if state.get("state") in {"done", "cancelled", "discarded", "failed"}:
                    self.redis.lrem(self.settings.processing_queue, 1, raw)
                    self.redis.lrem(self.settings.workload_queue, 1, job_id)
                    continue
                self.redis.lrem(self.settings.processing_queue, 1, raw)
                self.redis.rpush(self.settings.ready_queue, raw)
                self.redis.set(
                    self.job_key(job_id),
                    dumps({**job, "state": "pending", "recovered_at": now}),
                    ex=self.settings.queue_state_ttl_seconds,
                )
                recovered += 1
        return recovered

    def acquire_task_slot(self, task_id: str, job_id: str, limit: int) -> bool:
        key = self.task_key(task_id, "slots")
        now, expiry = time.time(), time.time() + self.settings.job_lease_seconds
        script = """
        redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
        if redis.call('ZSCORE', KEYS[1], ARGV[3]) then
          redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3]); return 1
        end
        if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then return 0 end
        redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3]); return 1
        """
        ok = bool(self.redis.eval(script, 1, key, now, expiry, job_id, int(limit)))
        if ok:
            self.redis.expire(key, self.settings.queue_state_ttl_seconds)
        return ok

    def pending_jobs(self, task_id: str) -> int:
        return int(self.redis.scard(self.task_key(task_id, "pending")) or 0)
