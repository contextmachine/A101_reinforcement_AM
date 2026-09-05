from __future__ import annotations

import contextlib
import time
import uuid
from typing import Any, Iterator, Mapping

try:
    import redis
except ImportError:  # pragma: no cover - installed in the production image
    redis = None

from .codec import decode_object, encode_object, sha256
from .config import Settings
from .jsonutil import dumps, loads, to_jsonable


class RedisStore:
    """Single Redis store for tasks, component work, solutions and worker jobs."""

    ENQUEUE_LUA = """
    local dedupe = KEYS[1]
    local ready = KEYS[2]
    local workload = KEYS[3]
    local pending = KEYS[4]
    if redis.call('SETNX', dedupe, ARGV[1]) == 0 then return 0 end
    redis.call('EXPIRE', dedupe, ARGV[2])
    redis.call('LPUSH', ready, ARGV[3])
    redis.call('LPUSH', workload, ARGV[3])
    redis.call('SADD', pending, ARGV[1])
    redis.call('EXPIRE', pending, ARGV[2])
    return 1
    """

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

    @staticmethod
    def dedupe_key(value: str) -> str:
        return f"rebar:job-dedupe:{sha256(value.encode())}"

    def _expire(self, *keys: str) -> None:
        if not keys:
            return
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
            except Exception:
                pass

    # ---------- task metadata ----------
    def create_task(self, task_id: str, meta: Mapping[str, Any], plan: Mapping[str, Any], input_obj: Any) -> None:
        meta_key = self.task_key(task_id, "meta")
        plan_key = self.task_key(task_id, "plan")
        pipe = self.redis.pipeline()
        pipe.set(meta_key, dumps(meta), ex=self.settings.task_ttl_seconds, nx=True)
        pipe.set(plan_key, dumps(plan), ex=self.settings.task_ttl_seconds, nx=True)
        pipe.set(self.task_key(task_id, "generation"), 0, ex=self.settings.task_ttl_seconds, nx=True)
        created = pipe.execute()
        if not created[0] or not created[1]:
            raise ValueError(f"Задача {task_id} уже существует")
        self.put_object(task_id, "input", input_obj)

    def get_meta(self, task_id: str) -> dict[str, Any] | None:
        return loads(self.redis.get(self.task_key(task_id, "meta")), None)

    def set_meta(self, task_id: str, meta: Mapping[str, Any]) -> None:
        self.redis.set(
            self.task_key(task_id, "meta"),
            dumps(meta),
            exat=int(meta.get("expires_at", time.time() + self.settings.task_ttl_seconds)),
        )

    def patch_meta(self, task_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock(f"task:{task_id}:meta"):
            meta = self.get_meta(task_id)
            if meta is None:
                raise KeyError(task_id)
            meta.update(to_jsonable(changes))
            meta["updated_at"] = time.time()
            self.set_meta(task_id, meta)
            return meta

    # Names used by the pipeline are intentionally aliases of the canonical task metadata.
    def task_meta(self, task_id: str) -> dict[str, Any]:
        return dict(self.get_meta(task_id) or {})

    def update_task_meta(self, task_id: str, **changes: Any) -> dict[str, Any]:
        return self.patch_meta(task_id, **changes)

    def get_plan(self, task_id: str) -> dict[str, Any]:
        plan = loads(self.redis.get(self.task_key(task_id, "plan")), None)
        if plan is None:
            raise KeyError(task_id)
        return plan

    def set_plan(self, task_id: str, plan: Mapping[str, Any]) -> None:
        self.redis.set(self.task_key(task_id, "plan"), dumps(plan), exat=self._task_expire_at(task_id))

    def add_requested_ns(self, task_id: str, ns: list[int]) -> dict[str, Any]:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        values = list(dict.fromkeys(int(n) for n in ns))
        if not values or any(n < 1 for n in values):
            raise ValueError("N должен быть положительным")
        if any(n > self.settings.max_n_value for n in values):
            raise ValueError(f"N превышает серверный лимит {self.settings.max_n_value}")
        with self.lock(f"task:{task_id}:plan"):
            plan = self.get_plan(task_id)
            order = list(map(int, plan.get("order", [])))
            for n in values:
                if n not in order:
                    order.append(n)
            if len(order) > self.settings.max_planned_n_values:
                raise ValueError(f"План превысит лимит {self.settings.max_planned_n_values} значений N")
            plan["order"] = order
            plan["paused"] = False
            plan["exhausted"] = False
            self.set_plan(task_id, plan)
        meta = self.get_meta(task_id) or {}
        requested = list(dict.fromkeys([*map(int, meta.get("requested_n", [])), *values]))
        self.patch_meta(task_id, requested_n=requested, state="running", cancelled=False)
        return plan

    # ---------- chunked objects ----------
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

    def _get_object_optional(self, task_id: str, name: str, default: Any = None) -> Any:
        try:
            return self.get_object(task_id, name)
        except KeyError:
            return default

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
        return [
            {"id": eid.decode() if isinstance(eid, bytes) else str(eid), **loads(fields.get(b"json"), {})}
            for eid, fields in rows
        ]

    def all_events(self, task_id: str, start: int = 0, limit: int = 10_000) -> list[dict[str, Any]]:
        rows = self.redis.xrange(self.task_key(task_id, "events"), min="-", max="+", count=max(1, int(limit)))
        values = [
            {"id": eid.decode() if isinstance(eid, bytes) else str(eid), **loads(fields.get(b"json"), {})}
            for eid, fields in rows
        ]
        return values[max(0, int(start)) :]

    # ---------- compatibility N states / results ----------
    def set_n_status(self, task_id: str, n: int, status: str, **extra: Any) -> None:
        key = self.task_key(task_id, "n-status")
        value = {"n": int(n), "status": status, "updated_at": time.time(), **to_jsonable(extra)}
        self.redis.hset(key, str(int(n)), dumps(value))
        self.redis.expireat(key, self._task_expire_at(task_id))

    def get_n_statuses(self, task_id: str) -> dict[str, dict[str, Any]]:
        raw = self.redis.hgetall(self.task_key(task_id, "n-status"))
        return {(k.decode() if isinstance(k, bytes) else str(k)): loads(v, {}) for k, v in raw.items()}

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

    def save_best_result(self, task_id: str, n: int, result: Mapping[str, Any], kind: str = "final") -> tuple[bool, dict[str, Any]]:
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

    # ---------- component pipeline data ----------
    def save_field(self, task_id: str, value: Mapping[str, Any]) -> None:
        self.put_object(task_id, "field", dict(value))

    def load_field(self, task_id: str) -> dict[str, Any]:
        return dict(self._get_object_optional(task_id, "field", {}) or {})

    def save_component(self, task_id: str, component_id: Any, value: Mapping[str, Any]) -> None:
        cid = str(component_id)
        self.put_object(task_id, f"component-{cid}", dict(value))
        key = self.task_key(task_id, "components")
        self.redis.sadd(key, cid)
        self.redis.expireat(key, self._task_expire_at(task_id))

    def load_component(self, task_id: str, component_id: Any) -> dict[str, Any] | None:
        value = self._get_object_optional(task_id, f"component-{component_id}", None)
        return None if value is None else dict(value)

    def component_ids(self, task_id: str) -> list[str]:
        values = self.redis.smembers(self.task_key(task_id, "components")) or []
        out = [v.decode() if isinstance(v, bytes) else str(v) for v in values]
        return sorted(out, key=lambda x: (x == "whole", int(x) if x.lstrip("-").isdigit() else x))

    def components(self, task_id: str) -> list[dict[str, Any]]:
        return [value for cid in self.component_ids(task_id) if (value := self.load_component(task_id, cid)) is not None]

    def save_problem(self, task_id: str, component_id: Any, value: Mapping[str, Any]) -> None:
        self.put_object(task_id, f"problem-{component_id}", dict(value))

    def load_problem(self, task_id: str, component_id: Any) -> dict[str, Any] | None:
        value = self._get_object_optional(task_id, f"problem-{component_id}", None)
        return None if value is None else dict(value)

    def save_solver_result(self, task_id: str, component_id: Any, n: int, value: Mapping[str, Any]) -> None:
        self.put_object(task_id, f"solver-{component_id}-{int(n)}", dict(value))

    def load_solver_result(self, task_id: str, component_id: Any, n: int) -> dict[str, Any] | None:
        value = self._get_object_optional(task_id, f"solver-{component_id}-{int(n)}", None)
        return None if value is None else dict(value)

    def save_frontier_result(self, task_id: str, component_id: Any, n: int, value: Mapping[str, Any]) -> None:
        cid = str(component_id)
        self.put_object(task_id, f"frontier-{cid}-{int(n)}", dict(value))
        n_key = self.task_key(task_id, f"frontier-n:{cid}")
        version_key = self.task_key(task_id, "frontier-version")
        pipe = self.redis.pipeline(transaction=False)
        pipe.sadd(n_key, int(n))
        pipe.incr(version_key)
        pipe.expireat(n_key, self._task_expire_at(task_id))
        pipe.expireat(version_key, self._task_expire_at(task_id))
        pipe.execute()

    def frontier_version(self, task_id: str) -> int:
        return int(self.redis.get(self.task_key(task_id, "frontier-version")) or 0)

    def load_frontier(self, task_id: str, component_id: Any) -> dict[int, dict[str, Any]]:
        cid = str(component_id)
        values = self.redis.smembers(self.task_key(task_id, f"frontier-n:{cid}")) or []
        out: dict[int, dict[str, Any]] = {}
        for raw in values:
            n = int(raw)
            value = self._get_object_optional(task_id, f"frontier-{cid}-{n}", None)
            if value is not None:
                out[n] = dict(value)
        return dict(sorted(out.items()))

    def all_frontiers(self, task_id: str, include_whole: bool = False) -> dict[Any, dict[int, dict[str, Any]]]:
        out: dict[Any, dict[int, dict[str, Any]]] = {}
        for cid in self.component_ids(task_id):
            if cid == "whole" and not include_whole:
                continue
            frontier = self.load_frontier(task_id, cid)
            if frontier:
                key: Any = int(cid) if cid.lstrip("-").isdigit() else cid
                out[key] = frontier
        return out

    def save_candidate(self, task_id: str, candidate_id: str, value: Mapping[str, Any]) -> None:
        self.put_object(task_id, f"candidate-{candidate_id}", dict(value))

    def load_candidate(self, task_id: str, candidate_id: str) -> dict[str, Any] | None:
        value = self._get_object_optional(task_id, f"candidate-{candidate_id}", None)
        return None if value is None else dict(value)

    def save_solution(self, task_id: str, solution: Mapping[str, Any]) -> None:
        row = dict(solution)
        sid = str(row["solution_id"])
        total_n = int(row["total_N"])
        source = str(row.get("source", "components"))
        score = float(row.get("actual_mass_kg", row.get("proxy_mass", float("inf"))))
        self.put_object(task_id, f"solution-{sid}", row)
        total_key = self.task_key(task_id, f"solutions:{total_n}")
        source_key = self.task_key(task_id, f"solution-source:{source}")
        all_key = self.task_key(task_id, "solution-ids")
        pipe = self.redis.pipeline(transaction=False)
        pipe.zadd(total_key, {sid: score})
        pipe.sadd(source_key, sid)
        pipe.sadd(all_key, sid)
        expires = self._task_expire_at(task_id)
        pipe.expireat(total_key, expires)
        pipe.expireat(source_key, expires)
        pipe.expireat(all_key, expires)
        pipe.execute()

    def load_solution(self, task_id: str, solution_id: str) -> dict[str, Any] | None:
        value = self._get_object_optional(task_id, f"solution-{solution_id}", None)
        return None if value is None else dict(value)

    def solutions(self, task_id: str, total_n: int | None = None, source: str | None = None) -> list[dict[str, Any]]:
        if total_n is not None:
            ids = self.redis.zrange(self.task_key(task_id, f"solutions:{int(total_n)}"), 0, -1) or []
        elif source is not None:
            ids = self.redis.smembers(self.task_key(task_id, f"solution-source:{source}")) or []
        else:
            ids = self.redis.smembers(self.task_key(task_id, "solution-ids")) or []
        decoded = [x.decode() if isinstance(x, bytes) else str(x) for x in ids]
        rows = [row for sid in decoded if (row := self.load_solution(task_id, sid)) is not None]
        if total_n is not None:
            rows = [r for r in rows if int(r.get("total_N", -1)) == int(total_n)]
        if source is not None:
            rows = [r for r in rows if str(r.get("source")) == str(source)]
        return sorted(
            rows,
            key=lambda r: (
                not bool(r.get("is_feasible")),
                float(r.get("actual_mass_kg", float("inf"))),
                float(r.get("proxy_mass", float("inf"))),
            ),
        )

    def best_solution(self, task_id: str, total_n: int) -> dict[str, Any] | None:
        rows = self.solutions(task_id, total_n=total_n)
        return rows[0] if rows else None

    # ---------- generation / cancellation / pause ----------
    def generation(self, task_id: str) -> int:
        return int(self.redis.get(self.task_key(task_id, "generation")) or 0)

    def bump_generation(self, task_id: str) -> int:
        key = self.task_key(task_id, "generation")
        value = int(self.redis.incr(key))
        self.redis.expireat(key, self._task_expire_at(task_id))
        return value

    def cancel_ns(self, task_id: str, ns: list[int]) -> None:
        key = self.task_key(task_id, "cancelled")
        targets = sorted({int(n) for n in ns if int(n) > 0})
        if targets:
            self.redis.sadd(key, *map(str, targets))
            self.redis.expireat(key, self._task_expire_at(task_id))
            for n in targets:
                self.set_n_status(task_id, n, "cancelled")
                self.publish_event(task_id, "n_cancelled", {"n": n})

    def is_n_cancelled(self, task_id: str, n: int) -> bool:
        meta = self.get_meta(task_id) or {}
        return bool(meta.get("cancelled")) or bool(
            self.redis.sismember(self.task_key(task_id, "cancelled"), str(int(n)))
        )

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        generation = self.bump_generation(task_id)
        meta = self.patch_meta(task_id, cancelled=True, state="cancelled", generation=generation)
        self.publish_event(task_id, "task_cancelled", {"generation": generation})
        return meta

    def set_paused(self, task_id: str, paused: bool) -> dict[str, Any]:
        with self.lock(f"task:{task_id}:plan"):
            plan = self.get_plan(task_id)
            plan["paused"] = bool(paused)
            self.set_plan(task_id, plan)
        meta = self.patch_meta(task_id, paused=bool(paused), state="paused" if paused else "running")
        self.publish_event(task_id, "range_paused" if paused else "range_resumed", {})
        return meta

    # ---------- single job queue ----------
    def enqueue_pipeline_job(self, job: Mapping[str, Any]) -> bool:
        row = dict(job)
        raw = dumps(row)
        job_id = str(row["job_id"])
        task_id = str(row["task_id"])
        dedupe = self.dedupe_key(str(row["dedupe_key"]))
        pending = self.task_key(task_id, "pending")
        try:
            result = self.redis.eval(
                self.ENQUEUE_LUA,
                4,
                dedupe,
                self.settings.ready_queue,
                self.settings.workload_queue,
                pending,
                job_id,
                self.settings.task_ttl_seconds,
                raw,
            )
            queued = bool(result)
        except Exception:
            if not self.redis.set(dedupe, job_id, nx=True, ex=self.settings.task_ttl_seconds):
                return False
            pipe = self.redis.pipeline(transaction=True)
            pipe.lpush(self.settings.ready_queue, raw)
            pipe.lpush(self.settings.workload_queue, raw)
            pipe.sadd(pending, job_id)
            pipe.expire(pending, self.settings.task_ttl_seconds)
            pipe.execute()
            queued = True
        if queued:
            self.redis.set(
                self.job_key(job_id),
                dumps({**row, "state": "pending"}),
                ex=self.settings.task_ttl_seconds,
            )
        return queued

    def claim_job(self, worker_id: str, timeout: int) -> tuple[str, dict[str, Any]] | None:
        raw = self.redis.brpoplpush(self.settings.ready_queue, self.settings.processing_queue, timeout=int(timeout))
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        job = loads(text, None)
        if not isinstance(job, dict):
            self.redis.lrem(self.settings.processing_queue, 1, raw)
            self.redis.lrem(self.settings.workload_queue, 1, raw)
            return None
        lease = self.job_key(str(job["job_id"])) + ":lease"
        self.redis.set(lease, worker_id, ex=self.settings.job_lease_seconds)
        state = {**job, "state": "claimed", "worker_id": worker_id, "claimed_at": time.time()}
        self.redis.set(self.job_key(str(job["job_id"])), dumps(state), ex=self.settings.task_ttl_seconds)
        return text, job

    def heartbeat_job(self, job: Mapping[str, Any], worker_id: str) -> None:
        lease = self.job_key(str(job["job_id"])) + ":lease"
        self.redis.set(lease, worker_id, ex=self.settings.job_lease_seconds)
        task_id = str(job["task_id"])
        self.redis.zadd(self.task_key(task_id, "slots"), {str(job["job_id"]): time.time() + self.settings.job_lease_seconds})
        self.redis.expire(self.task_key(task_id, "slots"), self.settings.task_ttl_seconds)

    def ack_job(self, raw: str, job: Mapping[str, Any], state: str = "done") -> None:
        task_id = str(job["task_id"])
        job_id = str(job["job_id"])
        self.redis.lrem(self.settings.processing_queue, 1, raw)
        self.redis.lrem(self.settings.workload_queue, 1, raw)
        self.redis.delete(self.job_key(job_id) + ":lease")
        self.redis.zrem(self.task_key(task_id, "slots"), job_id)
        self.redis.srem(self.task_key(task_id, "pending"), job_id)
        if job.get("dedupe_key"):
            self.redis.delete(self.dedupe_key(str(job["dedupe_key"])))
        self.redis.set(
            self.job_key(job_id),
            dumps({**job, "state": state, "finished_at": time.time()}),
            ex=self.settings.task_ttl_seconds,
        )

    def requeue_job(self, raw: str, job: Mapping[str, Any], delay: float = 0.0) -> None:
        self.redis.lrem(self.settings.processing_queue, 1, raw)
        self.redis.delete(self.job_key(str(job["job_id"])) + ":lease")
        self.redis.zrem(self.task_key(str(job["task_id"]), "slots"), str(job["job_id"]))
        self.redis.set(
            self.job_key(str(job["job_id"])),
            dumps({**job, "state": "pending"}),
            ex=self.settings.task_ttl_seconds,
        )
        if delay:
            time.sleep(delay)
        self.redis.lpush(self.settings.ready_queue, raw)

    def requeue_stale_jobs(self, grace_seconds: float = 10.0) -> int:
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
                if state.get("state") in {"done", "cancelled", "discarded", "failed"}:
                    self.redis.lrem(self.settings.processing_queue, 1, raw)
                    self.redis.lrem(self.settings.workload_queue, 1, raw)
                    continue
                self.redis.lrem(self.settings.processing_queue, 1, raw)
                self.redis.rpush(self.settings.ready_queue, raw)
                self.redis.set(
                    self.job_key(str(job["job_id"])),
                    dumps({**job, "state": "pending", "recovered_at": now}),
                    ex=self.settings.task_ttl_seconds,
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
            self.redis.expireat(key, self._task_expire_at(task_id))
        return ok

    def pending_jobs(self, task_id: str) -> int:
        return int(self.redis.scard(self.task_key(task_id, "pending")) or 0)

    def refresh_pipeline_state(self, task_id: str) -> dict[str, Any] | None:
        meta = self.get_meta(task_id)
        if meta is None:
            return None
        if meta.get("cancelled"):
            return meta
        if meta.get("paused"):
            return meta
        pending = self.pending_jobs(task_id)
        if pending:
            state = "running"
        elif meta.get("manual_mode"):
            # A manually controlled task is intentionally allowed to be idle with
            # zero solutions: the client can schedule component/whole N values later.
            state = "ready" if self.load_field(task_id) else "uploaded"
        else:
            state = "completed" if self.solutions(task_id) else "completed_with_errors"
        if state != meta.get("state"):
            meta = self.patch_meta(task_id, state=state)
            self.publish_event(task_id, "task_state", {"state": state})
        return meta

    # ---------- public snapshot ----------
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
