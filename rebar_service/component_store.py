from __future__ import annotations

import json
import os
import pickle
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .component_jobs import JobEnvelope


class ComponentStore:
    """Redis persistence for component workflow; legacy storage remains untouched."""

    ENQUEUE_LUA = """
    local dedupe = KEYS[1]
    local ready = KEYS[2]
    local workload = KEYS[3]
    local pending = KEYS[4]
    if redis.call('SETNX', dedupe, ARGV[1]) == 0 then return 0 end
    redis.call('EXPIRE', dedupe, ARGV[2])
    redis.call('RPUSH', ready, ARGV[3])
    redis.call('RPUSH', workload, ARGV[4])
    redis.call('SADD', pending, ARGV[5])
    return 1
    """

    def __init__(self, redis_client: Any, prefix: str = "rebar:component") -> None:
        self.redis = redis_client
        self.prefix = prefix.rstrip(":")
        self.ready_queue = os.getenv("REBAR_COMPONENT_READY_QUEUE", self.prefix + ":ready")
        self.processing_queue = os.getenv("REBAR_COMPONENT_PROCESSING_QUEUE", self.prefix + ":processing")
        self.workload_queue = os.getenv("REBAR_COMPONENT_WORKLOAD_QUEUE", self.prefix + ":workload")
        self.dedupe_ttl = int(os.getenv("REBAR_JOB_DEDUPE_TTL_SECONDS", "604800"))
        self.lease_seconds = int(os.getenv("REBAR_JOB_LEASE_SECONDS", "1800"))

    def key(self, *parts: Any) -> str:
        return ":".join([self.prefix, *(str(p) for p in parts)])

    @staticmethod
    def _dump(value: Any) -> bytes:
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def _load(value: Any, default: Any = None) -> Any:
        if value is None:
            return default
        try:
            return pickle.loads(value)
        except Exception:
            try:
                if isinstance(value, bytes):
                    value = value.decode("utf-8")
                return json.loads(value)
            except Exception:
                return default

    def put(self, key: str, value: Any) -> None:
        self.redis.set(key, self._dump(value))

    def get(self, key: str, default: Any = None) -> Any:
        return self._load(self.redis.get(key), default)

    def task_meta_key(self, task_id: str) -> str:
        return self.key("task", task_id, "meta")

    def update_task_meta(self, task_id: str, **values: Any) -> Dict[str, Any]:
        meta = dict(self.get(self.task_meta_key(task_id), {}) or {})
        meta.update(values)
        meta["updated_at"] = time.time()
        self.put(self.task_meta_key(task_id), meta)
        return meta

    def task_meta(self, task_id: str) -> Dict[str, Any]:
        return dict(self.get(self.task_meta_key(task_id), {}) or {})

    def save_request(self, task_id: str, request: Mapping[str, Any]) -> None:
        self.put(self.key("task", task_id, "request"), dict(request))

    def load_request(self, task_id: str) -> Dict[str, Any]:
        return dict(self.get(self.key("task", task_id, "request"), {}) or {})

    def save_field(self, task_id: str, value: Mapping[str, Any]) -> None:
        self.put(self.key("task", task_id, "field"), dict(value))

    def load_field(self, task_id: str) -> Dict[str, Any]:
        return dict(self.get(self.key("task", task_id, "field"), {}) or {})

    def save_component(self, task_id: str, component_id: Any, value: Mapping[str, Any]) -> None:
        cid = str(component_id)
        self.put(self.key("task", task_id, "component", cid), dict(value))
        self.redis.sadd(self.key("task", task_id, "components"), cid)

    def load_component(self, task_id: str, component_id: Any) -> Optional[Dict[str, Any]]:
        value = self.get(self.key("task", task_id, "component", component_id))
        return None if value is None else dict(value)

    def component_ids(self, task_id: str) -> List[str]:
        values = self.redis.smembers(self.key("task", task_id, "components")) or []
        out = [v.decode("utf-8") if isinstance(v, bytes) else str(v) for v in values]
        return sorted(out, key=lambda x: (x != "whole", int(x) if x.lstrip("-").isdigit() else x))

    def components(self, task_id: str) -> List[Dict[str, Any]]:
        return [value for cid in self.component_ids(task_id) if (value := self.load_component(task_id, cid)) is not None]

    def save_problem(self, task_id: str, component_id: Any, value: Mapping[str, Any]) -> None:
        self.put(self.key("task", task_id, "problem", component_id), dict(value))

    def load_problem(self, task_id: str, component_id: Any) -> Optional[Dict[str, Any]]:
        value = self.get(self.key("task", task_id, "problem", component_id))
        return None if value is None else dict(value)

    def save_solver_result(self, task_id: str, component_id: Any, n: int, value: Mapping[str, Any]) -> None:
        self.put(self.key("task", task_id, "solver", component_id, int(n)), dict(value))

    def load_solver_result(self, task_id: str, component_id: Any, n: int) -> Optional[Dict[str, Any]]:
        value = self.get(self.key("task", task_id, "solver", component_id, int(n)))
        return None if value is None else dict(value)

    def save_frontier_result(self, task_id: str, component_id: Any, n: int, value: Mapping[str, Any]) -> None:
        self.put(self.key("task", task_id, "frontier", component_id, int(n)), dict(value))
        self.redis.sadd(self.key("task", task_id, "frontier-n", component_id), int(n))
        self.redis.incr(self.key("task", task_id, "frontier-version"))

    def load_frontier(self, task_id: str, component_id: Any) -> Dict[int, Dict[str, Any]]:
        values = self.redis.smembers(self.key("task", task_id, "frontier-n", component_id)) or []
        out: Dict[int, Dict[str, Any]] = {}
        for raw in values:
            n = int(raw)
            value = self.get(self.key("task", task_id, "frontier", component_id, n))
            if value is not None:
                out[n] = dict(value)
        return dict(sorted(out.items()))

    def all_frontiers(self, task_id: str, include_whole: bool = False) -> Dict[Any, Dict[int, Dict[str, Any]]]:
        out: Dict[Any, Dict[int, Dict[str, Any]]] = {}
        for cid in self.component_ids(task_id):
            if cid == "whole" and not include_whole:
                continue
            frontier = self.load_frontier(task_id, cid)
            if frontier:
                key: Any = int(cid) if cid.lstrip("-").isdigit() else cid
                out[key] = frontier
        return out

    def save_candidate(self, task_id: str, candidate_id: str, value: Mapping[str, Any]) -> None:
        self.put(self.key("task", task_id, "candidate", candidate_id), dict(value))

    def load_candidate(self, task_id: str, candidate_id: str) -> Optional[Dict[str, Any]]:
        value = self.get(self.key("task", task_id, "candidate", candidate_id))
        return None if value is None else dict(value)

    def save_solution(self, task_id: str, solution: Mapping[str, Any]) -> None:
        row = dict(solution)
        sid = str(row["solution_id"])
        total_n = int(row["total_N"])
        source = str(row.get("source", "components"))
        score = float(row.get("actual_mass_kg", row.get("proxy_mass", float("inf"))))
        self.put(self.key("task", task_id, "solution", sid), row)
        self.redis.zadd(self.key("task", task_id, "solutions", total_n), {sid: score})
        self.redis.sadd(self.key("task", task_id, "solution-source", source), sid)
        self.redis.sadd(self.key("task", task_id, "solution-ids"), sid)

    def load_solution(self, task_id: str, solution_id: str) -> Optional[Dict[str, Any]]:
        value = self.get(self.key("task", task_id, "solution", solution_id))
        return None if value is None else dict(value)

    def solutions(self, task_id: str, total_n: Optional[int] = None, source: Optional[str] = None) -> List[Dict[str, Any]]:
        if total_n is not None:
            ids = self.redis.zrange(self.key("task", task_id, "solutions", int(total_n)), 0, -1) or []
        elif source is not None:
            ids = self.redis.smembers(self.key("task", task_id, "solution-source", source)) or []
        else:
            ids = self.redis.smembers(self.key("task", task_id, "solution-ids")) or []
        decoded = [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in ids]
        rows = [row for sid in decoded if (row := self.load_solution(task_id, sid)) is not None]
        if total_n is not None:
            rows = [r for r in rows if int(r.get("total_N", -1)) == int(total_n)]
        if source is not None:
            rows = [r for r in rows if str(r.get("source")) == str(source)]
        return sorted(rows, key=lambda r: (not bool(r.get("is_feasible")), float(r.get("actual_mass_kg", float("inf"))), float(r.get("proxy_mass", float("inf")))))

    def best_solution(self, task_id: str, total_n: int) -> Optional[Dict[str, Any]]:
        rows = self.solutions(task_id, total_n=total_n)
        return rows[0] if rows else None

    def publish(self, task_id: str, event: Mapping[str, Any]) -> None:
        row = {"task_id": task_id, "ts": time.time(), **dict(event)}
        raw = json.dumps(row, ensure_ascii=False, default=str)
        self.redis.rpush(self.key("task", task_id, "events"), raw)
        self.redis.publish(self.key("task", task_id, "events-channel"), raw)

    def events(self, task_id: str, start: int = 0) -> List[Dict[str, Any]]:
        rows = self.redis.lrange(self.key("task", task_id, "events"), int(start), -1) or []
        return [json.loads(v.decode("utf-8") if isinstance(v, bytes) else v) for v in rows]

    def generation(self, task_id: str) -> int:
        raw = self.redis.get(self.key("task", task_id, "generation"))
        return int(raw or 0)

    def bump_generation(self, task_id: str) -> int:
        return int(self.redis.incr(self.key("task", task_id, "generation")))

    def cancel(self, task_id: str) -> int:
        generation = self.bump_generation(task_id)
        self.update_task_meta(task_id, state="cancelled", generation=generation)
        self.publish(task_id, {"type": "cancelled", "generation": generation})
        return generation

    def enqueue(self, job: JobEnvelope) -> bool:
        raw = job.to_json()
        dedupe = self.key("job-dedupe", job.dedupe_key)
        pending = self.key("task", job.task_id, "pending")
        try:
            result = self.redis.eval(
                self.ENQUEUE_LUA,
                4,
                dedupe,
                self.ready_queue,
                self.workload_queue,
                pending,
                job.job_id,
                self.dedupe_ttl,
                raw,
                job.job_id,
                job.job_id,
            )
            return bool(result)
        except Exception:
            if not self.redis.set(dedupe, job.job_id, nx=True, ex=self.dedupe_ttl):
                return False
            pipe = self.redis.pipeline(transaction=True)
            pipe.rpush(self.ready_queue, raw)
            pipe.rpush(self.workload_queue, job.job_id)
            pipe.sadd(pending, job.job_id)
            pipe.execute()
            return True

    def claim(self, timeout: int = 5) -> Optional[JobEnvelope]:
        value = self.redis.brpoplpush(self.ready_queue, self.processing_queue, timeout=int(timeout))
        if value is None:
            return None
        job = JobEnvelope.from_value(value)
        self.redis.set(self.key("job-lease", job.job_id), value, ex=self.lease_seconds)
        return job

    def touch_lease(self, job: JobEnvelope) -> None:
        self.redis.expire(self.key("job-lease", job.job_id), self.lease_seconds)

    def recover_expired(self, limit: int = 1000) -> int:
        recovered = 0
        values = self.redis.lrange(self.processing_queue, 0, max(0, int(limit) - 1)) or []
        for raw in values:
            try:
                job = JobEnvelope.from_value(raw)
            except Exception:
                self.redis.lrem(self.processing_queue, 1, raw)
                continue
            if self.redis.exists(self.key("job-lease", job.job_id)):
                continue
            pipe = self.redis.pipeline(transaction=True)
            pipe.lrem(self.processing_queue, 1, raw)
            pipe.lpush(self.ready_queue, raw)
            pipe.execute()
            recovered += 1
        return recovered

    def ack(self, job: JobEnvelope) -> None:
        raw = job.to_json()
        self.redis.lrem(self.processing_queue, 1, raw)
        self.redis.srem(self.key("task", job.task_id, "pending"), job.job_id)
        self.redis.lrem(self.workload_queue, 1, job.job_id)
        self.redis.delete(self.key("job-lease", job.job_id))

    def fail(self, job: JobEnvelope, error: str) -> None:
        self.put(self.key("job-error", job.job_id), {"error": error, "job": job.to_dict(), "ts": time.time()})
        self.ack(job)
        self.publish(job.task_id, {"type": "job_failed", "job_id": job.job_id, "kind": job.kind, "error": error})
