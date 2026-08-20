from __future__ import annotations

import contextlib
import json
import time
import uuid
from typing import Any, Iterator, Mapping

try:
    import redis
except ImportError:  # pragma: no cover - dependency is installed in the image
    redis = None

from .codec import decode_object, encode_object, sha256
from .config import Settings
from .jsonutil import dumps, loads, to_jsonable


class RedisStore:
    def __init__(self, settings: Settings):
        if redis is None:
            raise ImportError("Установите пакет redis")
        self.settings = settings
        self.redis = redis.Redis.from_url(settings.redis_url, decode_responses=False)

    def ping(self) -> bool:
        return bool(self.redis.ping())

    @staticmethod
    def task_key(task_id: str, suffix: str) -> str:
        return f"rebar:task:{task_id}:{suffix}"

    @staticmethod
    def job_key(job_id: str) -> str:
        return f"rebar:job:{job_id}"

    def _expire(self, *keys: str) -> None:
        if keys:
            pipe = self.redis.pipeline(transaction=False)
            for key in keys:
                pipe.expire(key, self.settings.task_ttl_seconds)
            pipe.execute()

    def _task_expire_at(self, task_id: str) -> int:
        raw = loads(self.redis.get(self.task_key(task_id, "meta")), None)
        if raw is None:
            raise KeyError(task_id)
        expires = int(raw["expires_at"])
        if expires <= int(time.time()):
            raise TimeoutError(f"Задача {task_id} уже истекла")
        return expires

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
            except redis.RedisError:
                pass

    # ---------- task metadata ----------
    def create_task(self, task_id: str, meta: Mapping[str, Any], plan: Mapping[str, Any], input_obj: Any) -> None:
        meta_key = self.task_key(task_id, "meta")
        plan_key = self.task_key(task_id, "plan")
        pipe = self.redis.pipeline()
        pipe.set(meta_key, dumps(meta), ex=self.settings.task_ttl_seconds, nx=True)
        pipe.set(plan_key, dumps(plan), ex=self.settings.task_ttl_seconds, nx=True)
        created = pipe.execute()
        if not all(created):
            raise ValueError(f"Задача {task_id} уже существует")
        self.put_object(task_id, "input", input_obj)

    def get_meta(self, task_id: str) -> dict[str, Any] | None:
        return loads(self.redis.get(self.task_key(task_id, "meta")), None)

    def set_meta(self, task_id: str, meta: Mapping[str, Any]) -> None:
        self.redis.set(self.task_key(task_id, "meta"), dumps(meta), exat=int(meta.get("expires_at", time.time() + self.settings.task_ttl_seconds)))

    def patch_meta(self, task_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock(f"task:{task_id}:meta"):
            meta = self.get_meta(task_id)
            if meta is None:
                raise KeyError(task_id)
            meta.update(to_jsonable(changes))
            meta["updated_at"] = time.time()
            self.set_meta(task_id, meta)
            return meta

    def get_plan(self, task_id: str) -> dict[str, Any]:
        plan = loads(self.redis.get(self.task_key(task_id, "plan")), None)
        if plan is None:
            raise KeyError(task_id)
        return plan

    def set_plan(self, task_id: str, plan: Mapping[str, Any]) -> None:
        self.redis.set(self.task_key(task_id, "plan"), dumps(plan), exat=self._task_expire_at(task_id))

    # ---------- chunked blobs ----------
    def put_object(self, task_id: str, name: str, value: Any) -> dict[str, Any]:
        payload, codec = encode_object(value)
        return self.put_blob(task_id, name, payload, codec)

    def get_object(self, task_id: str, name: str) -> Any:
        return decode_object(self.get_blob(task_id, name))

    def put_blob(self, task_id: str, name: str, payload: bytes, codec: str = "bytes") -> dict[str, Any]:
        base = self.task_key(task_id, f"blob:{name}")
        meta_key = f"{base}:meta"
        chunk_size = self.settings.blob_chunk_bytes
        chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)] or [b""]
        old = loads(self.redis.get(meta_key), {})
        expires = self._task_expire_at(task_id)
        pipe = self.redis.pipeline(transaction=False)
        for i, chunk in enumerate(chunks):
            pipe.set(f"{base}:{i}", chunk, exat=expires)
        meta = {"chunks": len(chunks), "bytes": len(payload), "sha256": sha256(payload), "codec": codec}
        pipe.set(meta_key, dumps(meta), exat=expires)
        pipe.execute()
        for i in range(int(old.get("chunks", 0))):
            if i >= len(chunks):
                self.redis.delete(f"{base}:{i}")
        return meta

    def get_blob(self, task_id: str, name: str) -> bytes:
        base = self.task_key(task_id, f"blob:{name}")
        meta = loads(self.redis.get(f"{base}:meta"), None)
        if meta is None:
            raise KeyError(f"blob {name} для task={task_id} не найден")
        keys = [f"{base}:{i}" for i in range(int(meta["chunks"]))]
        parts = self.redis.mget(keys)
        if any(part is None for part in parts):
            raise IOError(f"blob {name} повреждён: отсутствуют chunks")
        payload = b"".join(parts)
        if len(payload) != int(meta["bytes"]) or sha256(payload) != meta["sha256"]:
            raise IOError(f"blob {name} повреждён")
        return payload

    def delete_blob(self, task_id: str, name: str) -> None:
        base = self.task_key(task_id, f"blob:{name}")
        meta_key = f"{base}:meta"
        meta = loads(self.redis.get(meta_key), {})
        keys = [meta_key, *(f"{base}:{i}" for i in range(int(meta.get("chunks", 0))))]
        if keys:
            self.redis.delete(*keys)

    # ---------- events ----------
    def publish_event(self, task_id: str, event_type: str, payload: Mapping[str, Any]) -> str:
        key = self.task_key(task_id, "events")
        body = {"type": event_type, "task_id": task_id, "time": time.time(), **to_jsonable(payload)}
        event_id = self.redis.xadd(
            key,
            {b"json": dumps(body).encode()},
            maxlen=self.settings.event_maxlen,
            approximate=True,
        )
        self.redis.expireat(key, self._task_expire_at(task_id))
        return event_id.decode() if isinstance(event_id, bytes) else str(event_id)

    def read_events(self, task_id: str, after: str = "0-0", count: int = 200) -> list[dict[str, Any]]:
        rows = self.redis.xrange(self.task_key(task_id, "events"), min=f"({after}", max="+", count=count)
        return [{"id": eid.decode() if isinstance(eid, bytes) else str(eid), **loads(fields.get(b"json"), {})} for eid, fields in rows]

    # ---------- N states / results ----------
    def set_n_status(self, task_id: str, n: int, status: str, **extra: Any) -> None:
        key = self.task_key(task_id, "n-status")
        value = {"n": int(n), "status": status, "updated_at": time.time(), **to_jsonable(extra)}
        self.redis.hset(key, str(int(n)), dumps(value))
        self.redis.expireat(key, self._task_expire_at(task_id))

    def get_n_statuses(self, task_id: str) -> dict[str, dict[str, Any]]:
        raw = self.redis.hgetall(self.task_key(task_id, "n-status"))
        return {
            (k.decode() if isinstance(k, bytes) else str(k)): loads(v, {})
            for k, v in raw.items()
        }

    @staticmethod
    def _result_rank(meta: Mapping[str, Any]) -> tuple[int, int, float, int]:
        cost = meta.get("total_cost")
        feasible = bool(meta.get("is_feasible"))
        postprocessed = bool(meta.get("postprocessed"))
        tier = 0 if feasible and postprocessed else (1 if feasible else 2)
        return (
            tier,
            0 if meta.get("is_optimal") else 1,
            float("inf") if cost is None else float(cost),
            0 if meta.get("kind") == "final" else 1,
        )

    def save_best_result(self, task_id: str, n: int, result: Mapping[str, Any], kind: str) -> tuple[bool, dict[str, Any]]:
        solver_result = result.get("solver_result") if isinstance(result.get("solver_result"), Mapping) else {}
        total_cost = result.get("total_cost")
        if total_cost is None:
            total_cost = solver_result.get("total_cost")
        meta = {
            "n": int(n),
            "kind": kind,
            "is_feasible": bool(result.get("is_feasible", solver_result.get("is_feasible", False))),
            "is_optimal": bool(result.get("is_optimal", solver_result.get("is_optimal", False))),
            "total_cost": None if total_cost is None else float(total_cost),
            "postprocessed": bool(result.get("fit_result") is not None or result.get("summary") is not None),
            "updated_at": time.time(),
        }
        pointer_key = self.task_key(task_id, "result-meta")
        with self.lock(f"task:{task_id}:result:{n}"):
            old = loads(self.redis.hget(pointer_key, str(n)), None)
            if old is not None and self._result_rank(old) <= self._result_rank(meta):
                return False, old
            blob_name = f"result-{n}-{uuid.uuid4().hex}"
            self.put_object(task_id, blob_name, dict(result))
            meta["blob"] = blob_name
            self.redis.hset(pointer_key, str(n), dumps(meta))
            self.redis.expireat(pointer_key, self._task_expire_at(task_id))
            if old and old.get("blob"):
                self.delete_blob(task_id, old["blob"])
            return True, meta

    def get_result_meta(self, task_id: str, n: int) -> dict[str, Any] | None:
        return loads(self.redis.hget(self.task_key(task_id, "result-meta"), str(int(n))), None)

    def get_result(self, task_id: str, n: int) -> dict[str, Any] | None:
        meta = self.get_result_meta(task_id, n)
        return None if meta is None else self.get_object(task_id, meta["blob"])

    def get_result_metas(self, task_id: str) -> dict[str, dict[str, Any]]:
        raw = self.redis.hgetall(self.task_key(task_id, "result-meta"))
        return {(k.decode() if isinstance(k, bytes) else str(k)): loads(v, {}) for k, v in raw.items()}

    # ---------- compact solver data ----------
    @staticmethod
    def merge_data_values(current: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None) -> dict[str, Any]:
        a, b = dict(current or {}), dict(incoming or {})
        problem_id = b.get("problem_id") or a.get("problem_id")
        if a.get("problem_id") and b.get("problem_id") and a["problem_id"] != b["problem_id"]:
            raise ValueError("Нельзя объединить solver data разных problem_id")
        out = {"schema_version": max(int(a.get("schema_version", 0)), int(b.get("schema_version", 0)), 3), "problem_id": problem_id, "solutions": {}, "infeasible": []}
        for source in (a, b):
            for n, entry in dict(source.get("solutions", {})).items():
                if not isinstance(entry, Mapping) or not entry.get("indices"):
                    continue
                old = out["solutions"].get(str(n))
                cost = entry.get("cost")
                rank = (0 if entry.get("optimal") else 1, float("inf") if cost is None else float(cost))
                old_cost = None if old is None else old.get("cost")
                old_rank = (9, float("inf")) if old is None else (
                    0 if old.get("optimal") else 1,
                    float("inf") if old_cost is None else float(old_cost),
                )
                if rank < old_rank:
                    out["solutions"][str(n)] = to_jsonable(entry)
        infeasible = {int(x) for source in (a, b) for x in source.get("infeasible", [])}
        infeasible -= {int(n) for n in out["solutions"]}
        out["infeasible"] = sorted(infeasible)
        return out

    def get_solver_data(self, task_id: str) -> dict[str, Any]:
        return loads(self.redis.get(self.task_key(task_id, "solver-data")), {})

    def merge_solver_data(self, task_id: str, incoming: Mapping[str, Any]) -> dict[str, Any]:
        key = self.task_key(task_id, "solver-data")
        with self.lock(f"task:{task_id}:solver-data"):
            merged = self.merge_data_values(loads(self.redis.get(key), {}), incoming)
            self.redis.set(key, dumps(merged), exat=self._task_expire_at(task_id))
            return merged

    # ---------- cancellation ----------
    def cancel_ns(self, task_id: str, ns: list[int]) -> None:
        key = self.task_key(task_id, "cancelled")
        statuses = self.get_n_statuses(task_id)
        terminal = {"optimal", "feasible", "infeasible"}
        targets = sorted({int(n) for n in ns if statuses.get(str(int(n)), {}).get("status") not in terminal})
        if targets:
            self.redis.sadd(key, *map(str, targets))
            self.redis.expireat(key, self._task_expire_at(task_id))
            for n in targets:
                self.set_n_status(task_id, n, "cancelled")
                self.publish_event(task_id, "n_cancelled", {"n": n})

    def is_n_cancelled(self, task_id: str, n: int) -> bool:
        meta = self.get_meta(task_id) or {}
        return bool(meta.get("cancelled")) or bool(self.redis.sismember(self.task_key(task_id, "cancelled"), str(int(n))))

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        plan = self.get_plan(task_id)
        statuses = self.get_n_statuses(task_id)
        terminal = {"optimal", "feasible", "infeasible"}
        ns = sorted(
            n for n in set(map(int, plan.get("order", [])))
            if statuses.get(str(n), {}).get("status") not in terminal
        )
        cancelled_key = self.task_key(task_id, "cancelled")
        status_key = self.task_key(task_id, "n-status")
        expires = self._task_expire_at(task_id)
        pipe = self.redis.pipeline(transaction=False)
        if ns:
            pipe.sadd(cancelled_key, *map(str, ns))
            now = time.time()
            for n in ns:
                pipe.hset(status_key, str(n), dumps({"n": n, "status": "cancelled", "updated_at": now}))
            pipe.expireat(cancelled_key, expires)
            pipe.expireat(status_key, expires)
        pipe.execute()
        meta = self.patch_meta(task_id, cancelled=True, state="cancelled")
        self.publish_event(task_id, "task_cancelled", {"n_count": len(ns)})
        return meta

    # ---------- queue / jobs ----------
    def enqueue_job(self, task_id: str, kind: str, n: int | None = None, attempt: int = 0) -> dict[str, Any]:
        job = {"job_id": uuid.uuid4().hex, "task_id": task_id, "kind": kind, "n": n, "attempt": int(attempt), "created_at": time.time()}
        raw = dumps(job)
        pipe = self.redis.pipeline(transaction=True)
        pipe.set(self.job_key(job["job_id"]), dumps({**job, "state": "pending"}), ex=self.settings.task_ttl_seconds, nx=True)
        pipe.lpush(self.settings.ready_queue, raw)
        # KEDA scales on total outstanding work. Unlike ready_queue, this entry
        # stays present while the job is in processing_queue and is removed only
        # by ack_job(). This avoids under-scaling while all workers are busy.
        pipe.lpush(self.settings.workload_queue, raw)
        pipe.execute()
        return job

    def claim_job(self, worker_id: str, timeout: int) -> tuple[str, dict[str, Any]] | None:
        raw = self.redis.brpoplpush(self.settings.ready_queue, self.settings.processing_queue, timeout=timeout)
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        job = loads(text, None)
        if not isinstance(job, dict):
            self.redis.lrem(self.settings.processing_queue, 1, raw)
            self.redis.lrem(self.settings.workload_queue, 1, raw)
            return None
        lease = self.job_key(job["job_id"]) + ":lease"
        self.redis.set(lease, worker_id, ex=self.settings.job_lease_seconds)
        state = {**job, "state": "claimed", "worker_id": worker_id, "claimed_at": time.time()}
        self.redis.set(self.job_key(job["job_id"]), dumps(state), ex=self.settings.task_ttl_seconds)
        return text, job

    def heartbeat_job(self, job: Mapping[str, Any], worker_id: str) -> None:
        lease = self.job_key(str(job["job_id"])) + ":lease"
        self.redis.set(lease, worker_id, ex=self.settings.job_lease_seconds)
        task_id = str(job["task_id"])
        self.redis.zadd(self.task_key(task_id, "slots"), {str(job["job_id"]): time.time() + self.settings.job_lease_seconds})
        self.redis.expire(self.task_key(task_id, "slots"), self.settings.task_ttl_seconds)

    def ack_job(self, raw: str, job: Mapping[str, Any], state: str = "done") -> None:
        self.redis.lrem(self.settings.processing_queue, 1, raw)
        self.redis.lrem(self.settings.workload_queue, 1, raw)
        self.redis.delete(self.job_key(str(job["job_id"])) + ":lease")
        self.redis.zrem(self.task_key(str(job["task_id"]), "slots"), str(job["job_id"]))
        self.redis.set(self.job_key(str(job["job_id"])), dumps({**job, "state": state, "finished_at": time.time()}), ex=self.settings.task_ttl_seconds)

    def requeue_job(self, raw: str, job: Mapping[str, Any], delay: float = 0.0) -> None:
        self.redis.lrem(self.settings.processing_queue, 1, raw)
        self.redis.delete(self.job_key(str(job["job_id"])) + ":lease")
        self.redis.zrem(self.task_key(str(job["task_id"]), "slots"), str(job["job_id"]))
        self.redis.set(self.job_key(str(job["job_id"])), dumps({**job, "state": "pending"}), ex=self.settings.task_ttl_seconds)
        if delay:
            time.sleep(delay)
        # Producers LPUSH and workers BRPOP. LPUSH here puts a throttled job
        # behind already queued work instead of immediately reclaiming it.
        self.redis.lpush(self.settings.ready_queue, raw)

    def requeue_stale_jobs(self, grace_seconds: float = 10.0) -> int:
        """Return jobs whose worker lease disappeared after a pod/process crash."""
        recovered = 0
        with self.lock("queue-reaper", timeout=2.0):
            now = time.time()
            for raw in self.redis.lrange(self.settings.processing_queue, 0, -1):
                text = raw.decode() if isinstance(raw, bytes) else str(raw)
                job = loads(text, None)
                if not isinstance(job, dict):
                    self.redis.lrem(self.settings.processing_queue, 1, raw)
                    continue
                lease_key = self.job_key(str(job["job_id"])) + ":lease"
                if self.redis.exists(lease_key):
                    continue
                state = loads(self.redis.get(self.job_key(str(job["job_id"]))), {})
                claimed_at = float(state.get("claimed_at", job.get("created_at", 0)))
                if now - claimed_at < self.settings.job_lease_seconds + grace_seconds:
                    continue
                if state.get("state") in {"done", "cancelled", "discarded"}:
                    self.redis.lrem(self.settings.processing_queue, 1, raw)
                    self.redis.lrem(self.settings.workload_queue, 1, raw)
                    continue
                self.redis.lrem(self.settings.processing_queue, 1, raw)
                self.redis.rpush(self.settings.ready_queue, raw)
                self.redis.set(self.job_key(str(job["job_id"])), dumps({**job, "state": "pending", "recovered_at": now}), ex=self.settings.task_ttl_seconds)
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
            self.redis.expireat(key, self._task_expire_at(task_id))
        return ok

    # ---------- plan / scheduling ----------
    def add_ns(self, task_id: str, ns: list[int]) -> dict[str, Any]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        max_n = int(meta.get("parameters", {}).get("solver", {}).get("prepared_max_n", 0))
        status_key = self.task_key(task_id, "n-status")
        cancelled_key = self.task_key(task_id, "cancelled")
        with self.lock(f"task:{task_id}:plan"):
            plan = self.get_plan(task_id)
            order = list(map(int, plan.get("order", [])))
            seen = set(order)
            statuses = self.get_n_statuses(task_id)
            additions = {int(n) for n in ns if int(n) not in seen}
            if len(order) + len(additions) > self.settings.max_planned_n_values:
                raise ValueError(
                    f"План превысит лимит {self.settings.max_planned_n_values} значений N"
                )
            for n in ns:
                n = int(n)
                if n < 0:
                    raise ValueError("N должен быть неотрицательным")
                if n > self.settings.max_n_value:
                    raise ValueError(f"N={n} превышает серверный лимит {self.settings.max_n_value}")
                if max_n and n > max_n:
                    raise ValueError(f"N={n} превышает prepared_max_n={max_n}")
                current = statuses.get(str(n), {}).get("status")
                if n not in seen:
                    order.append(n); seen.add(n)
                elif current in {"error", "cancelled", "postprocess_infeasible", "feasible", "incumbent"}:
                    order.append(n)  # explicit add acts as retry
                    self.redis.hdel(status_key, str(n))
                    self.redis.srem(cancelled_key, str(n))
            plan["order"] = order
            plan["exhausted"] = False
            self.set_plan(task_id, plan)
        self.patch_meta(task_id, state="running", cancelled=False)
        self.refill_task(task_id)
        return plan

    def refill_task(self, task_id: str) -> int:
        with self.lock(f"task:{task_id}:schedule"):
            meta = self.get_meta(task_id)
            if not meta or meta.get("cancelled") or meta.get("state") in {"failed", "preparing", "queued_preparation"}:
                return 0
            plan = self.get_plan(task_id)
            if plan.get("paused"):
                return 0
            statuses = self.get_n_statuses(task_id)
            active = sum(1 for v in statuses.values() if v.get("status") in {"queued", "running", "incumbent"})
            window = int(plan.get("window", self.settings.schedule_window))
            cursor, order, queued = int(plan.get("cursor", 0)), list(map(int, plan.get("order", []))), 0
            while cursor < len(order) and active < window:
                n = order[cursor]
                cursor += 1
                status = statuses.get(str(n), {}).get("status")
                if status in {"queued", "running", "optimal", "feasible", "infeasible", "cancelled"} or self.is_n_cancelled(task_id, n):
                    continue
                self.enqueue_job(task_id, "solve", n)
                self.set_n_status(task_id, n, "queued")
                self.publish_event(task_id, "n_queued", {"n": n})
                active += 1
                queued += 1
            plan["cursor"] = cursor
            plan["exhausted"] = cursor >= len(order)
            self.set_plan(task_id, plan)
            return queued

    def refresh_task_state(self, task_id: str) -> dict[str, Any]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        plan, statuses = self.get_plan(task_id), self.get_n_statuses(task_id)
        active = any(v.get("status") in {"queued", "running", "incumbent"} for v in statuses.values())
        if meta.get("cancelled"):
            state = "cancelled"
        elif meta.get("state") == "failed":
            state = "failed"
        elif meta.get("state") in {"queued_preparation", "preparing"}:
            state = meta["state"]
        elif active:
            state = "running"
        elif plan.get("paused"):
            state = "paused"
        elif plan.get("exhausted"):
            has_errors = any(
                value.get("status") in {"error", "postprocess_infeasible"}
                for value in statuses.values()
            )
            state = "completed_with_errors" if has_errors else "completed"
        else:
            state = "ready"
        if state != meta.get("state"):
            meta = self.patch_meta(task_id, state=state)
            self.publish_event(task_id, "task_state", {"state": state})
        return meta

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        meta = self.get_meta(task_id)
        if meta is None:
            return None
        statuses = self.get_n_statuses(task_id)
        counts: dict[str, int] = {}
        for value in statuses.values():
            status = str(value.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "task": meta,
            "plan": self.get_plan(task_id),
            "n": statuses,
            "status_counts": counts,
            "results": self.get_result_metas(task_id),
        }
