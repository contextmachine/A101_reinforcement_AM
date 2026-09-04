from __future__ import annotations

import os
import shutil
import signal
import socket
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping

from A101.rectangle_solver_job import solve_rectangle_job
from A101.rectangle_solver_stream import start_rectangle_job
from A101.select_min_density_rectangles_recipes import save_prepared_rectangle_problem

from .config import get_settings
from .pipeline import finalize_solution, prepare_pipeline
from .store import RedisStore


def _optional_timeout(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
        return None
    return float(value)


class PreparedCache:
    def __init__(self, root: str, limit: int):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.limit = max(1, int(limit))
        self.paths: OrderedDict[str, Path] = OrderedDict()
        self.contexts: OrderedDict[str, Any] = OrderedDict()

    def _trim(self) -> None:
        while len(self.paths) > self.limit:
            task_id, path = self.paths.popitem(last=False)
            self.contexts.pop(task_id, None)
            shutil.rmtree(path.parent, ignore_errors=True)

    def load(self, store: RedisStore, task_id: str):
        if task_id in self.paths and self.paths[task_id].exists():
            self.paths.move_to_end(task_id)
            self.contexts.move_to_end(task_id)
            return str(self.paths[task_id]), self.contexts[task_id]
        directory = self.root / task_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "prepared.pkl"
        prepared = store.get_object(task_id, "prepared")
        context = store.get_object(task_id, "context")
        save_prepared_rectangle_problem(prepared, path)
        self.paths[task_id], self.contexts[task_id] = path, context
        self._trim()
        return str(path), context


class LeaseHeartbeat:
    def __init__(self, store: RedisStore, job: Mapping[str, Any], worker_id: str):
        self.store, self.job, self.worker_id = store, job, worker_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        interval = max(5.0, self.store.settings.job_lease_seconds / 3)
        while not self.stop_event.wait(interval):
            try:
                self.store.heartbeat_job(self.job, self.worker_id)
            except Exception:
                pass

    def __enter__(self):
        self.store.heartbeat_job(self.job, self.worker_id)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=2)


def _incumbent_data(prepared_problem_id: str, n: int, result: Mapping[str, Any], optimal: bool = False):
    if not result.get("indices"):
        return {}
    entry = {"indices": list(map(int, result["indices"])), "cost": float(result["total_cost"])}
    if optimal:
        entry.update(optimal=True, v=1)
    return {"schema_version": 3, "problem_id": prepared_problem_id, "solutions": {str(n): entry}, "infeasible": []}


def _process_prepare(store: RedisStore, task_id: str) -> None:
    existing = store.get_meta(task_id)
    if existing and existing.get("problem_id") and existing.get("state") not in {"queued_preparation", "preparing"}:
        store.refill_task(task_id)
        store.refresh_task_state(task_id)
        return
    meta = store.patch_meta(task_id, state="preparing")
    store.publish_event(task_id, "preparation_started", {})
    input_payload = store.get_object(task_id, "input")

    def progress(phase, payload):
        store.publish_event(task_id, "preparation_progress", {"phase": phase, **payload})

    prepared, context, public = prepare_pipeline(input_payload, meta["parameters"], progress)
    store.put_object(task_id, "prepared", prepared)
    store.put_object(task_id, "context", context)
    store.merge_solver_data(task_id, {"schema_version": 3, "problem_id": prepared["problem_id"], "solutions": {}, "infeasible": []})
    store.patch_meta(task_id, state="ready", problem_id=prepared["problem_id"], preparation=public)
    store.publish_event(task_id, "prepared", public)
    store.refill_task(task_id)
    store.refresh_task_state(task_id)


def _publish_incumbent(
    store: RedisStore,
    task_id: str,
    n: int,
    event: Mapping[str, Any],
    prepared_id: str,
    context: Mapping[str, Any],
    postprocess: bool,
) -> None:
    result = event.get("result") or {}
    if not result.get("is_feasible"):
        return
    stored = result
    postprocessed = False
    if postprocess:
        candidate = finalize_solution(result, context)
        if candidate.get("is_feasible"):
            stored, postprocessed = candidate, True
    saved, meta = store.save_best_result(task_id, n, stored, "incumbent")
    store.merge_solver_data(task_id, _incumbent_data(prepared_id, n, result))
    store.set_n_status(
        task_id,
        n,
        "incumbent",
        total_cost=result.get("total_cost"),
        gap=event.get("gap"),
        nodes=event.get("nodes"),
        elapsed=event.get("elapsed"),
    )
    store.publish_event(
        task_id,
        "incumbent",
        {
            "n": n,
            "saved": saved,
            "total_cost": result.get("total_cost"),
            "gap": event.get("gap"),
            "nodes": event.get("nodes"),
            "elapsed": event.get("elapsed"),
            "is_optimal": False,
            "postprocessed": postprocessed,
            "result_url": f"/v1/tasks/{task_id}/results/{n}",
            "result_meta": meta,
        },
    )


def _process_solve(store: RedisStore, cache: PreparedCache, task_id: str, n: int) -> None:
    current = store.get_n_statuses(task_id).get(str(n), {}).get("status")
    if current in {"optimal", "infeasible"}:
        return
    if store.is_n_cancelled(task_id, n):
        store.set_n_status(task_id, n, "cancelled")
        return
    meta = store.get_meta(task_id)
    if meta is None:
        raise KeyError(task_id)
    opts = dict(meta["parameters"].get("solver", {}))
    prepared_path, context = cache.load(store, task_id)
    solver_data = store.get_solver_data(task_id)
    store.set_n_status(task_id, n, "running", started_at=time.time())
    store.publish_event(task_id, "n_started", {"n": n, "backend": opts.get("backend", "highs")})

    backend = opts.get("backend", "highs")
    if backend == "highs":
        job = start_rectangle_job(
            prepared=prepared_path,
            data=solver_data,
            N=n,
            timeout=_optional_timeout(opts.get("timeout_seconds")),
            solver_time_limit=opts.get("solver_time_limit"),
            threads=int(opts.get("threads", 1)),
            require_optimal=bool(opts.get("require_optimal", True)),
            use_warm_start=bool(opts.get("use_warm_start", True)),
            cross_n_warm_start=bool(opts.get("cross_n_warm_start", True)),
            return_best_on_timeout=bool(opts.get("return_best_on_timeout", True)),
            emit_interval=float(opts.get("emit_interval", 5.0)),
            emit_every_nodes=opts.get("emit_every_nodes"),
            emit_on_improvement=True,
            emit_heartbeat=bool(opts.get("emit_heartbeat", True)),
            highs_options=opts.get("highs_options", {}),
            raise_worker_errors=True,
        )
        while job.is_alive():
            if store.is_n_cancelled(task_id, n):
                job.cancel()
                store.set_n_status(task_id, n, "cancelled")
                store.publish_event(task_id, "n_cancelled", {"n": n, "running": True})
                return
            event = job.poll(timeout=0.5)
            if not event:
                continue
            if event.get("type") == "incumbent":
                _publish_incumbent(
                    store,
                    task_id,
                    n,
                    event,
                    job.problem_id,
                    context,
                    bool(opts.get("postprocess_intermediate", False)),
                )
            elif event.get("type") == "heartbeat":
                store.publish_event(task_id, "solver_heartbeat", {"n": n, **{k: event.get(k) for k in ("elapsed", "best_incumbent_cost", "best_bound", "gap", "nodes")}})
        solver_result, updated_data = job.result()
    else:
        solver_result, updated_data = solve_rectangle_job(
            prepared=prepared_path,
            data=solver_data,
            N=n,
            timeout=_optional_timeout(opts.get("timeout_seconds")),
            solver_time_limit=opts.get("solver_time_limit"),
            threads=int(opts.get("threads", 1)),
            backend="pulp",
            pulp_solver="cbc",
            require_optimal=bool(opts.get("require_optimal", True)),
            use_warm_start=bool(opts.get("use_warm_start", True)),
            cross_n_warm_start=bool(opts.get("cross_n_warm_start", True)),
            return_best_on_timeout=bool(opts.get("return_best_on_timeout", True)),
        )

    store.merge_solver_data(task_id, updated_data or {})
    if not solver_result:
        store.set_n_status(task_id, n, "error", error="solver returned None")
        store.publish_event(task_id, "n_error", {"n": n, "error": "solver returned None"})
        return
    if solver_result.get("infeasibility_proved"):
        store.set_n_status(task_id, n, "infeasible")
        store.publish_event(task_id, "n_finished", {"n": n, "status": "infeasible"})
        return
    if not solver_result.get("is_feasible"):
        store.set_n_status(task_id, n, "error", error=solver_result.get("reason"), solver_status=solver_result.get("status"))
        store.publish_event(task_id, "n_error", {"n": n, "result": solver_result})
        return

    full = finalize_solution(solver_result, context)
    saved, result_meta = store.save_best_result(task_id, n, full, "final")
    status = "optimal" if full.get("is_optimal") else ("feasible" if full.get("is_feasible") else "postprocess_infeasible")
    store.set_n_status(
        task_id,
        n,
        status,
        total_cost=solver_result.get("total_cost"),
        termination_reason=solver_result.get("termination_reason"),
        result_saved=saved,
    )
    store.publish_event(
        task_id,
        "n_finished",
        {
            "n": n,
            "status": status,
            "is_optimal": bool(full.get("is_optimal")),
            "is_feasible": bool(full.get("is_feasible")),
            "total_cost": solver_result.get("total_cost"),
            "termination_reason": solver_result.get("termination_reason"),
            "result_url": f"/v1/tasks/{task_id}/results/{n}",
            "result_meta": result_meta,
        },
    )


def run_worker() -> None:
    settings = get_settings()
    store = RedisStore(settings)
    store.ping()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    cache = PreparedCache(settings.local_cache_dir, settings.local_cache_items)
    stop_event = threading.Event()
    last_reap = 0.0

    def request_stop(_signum, _frame):
        # KEDA/HPA may terminate any replica during scale-down. Do not claim
        # new work after SIGTERM; an already running solve is allowed to finish.
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    while not stop_event.is_set():
        claimed = store.claim_job(worker_id, settings.worker_claim_timeout_seconds)
        if claimed is None:
            if time.monotonic() - last_reap >= 30:
                try:
                    store.requeue_stale_jobs()
                except Exception:
                    pass
                last_reap = time.monotonic()
            continue
        raw, job = claimed
        if stop_event.is_set():
            store.requeue_job(raw, job)
            break
        task_id, kind = str(job["task_id"]), str(job["kind"])
        meta = store.get_meta(task_id)
        if meta is None or meta.get("cancelled"):
            store.ack_job(raw, job, "discarded")
            continue
        limit = int(meta.get("max_concurrent_jobs") or settings.max_jobs_per_task)
        if not store.acquire_task_slot(task_id, str(job["job_id"]), limit):
            store.requeue_job(raw, job, delay=0.2)
            continue

        try:
            with LeaseHeartbeat(store, job, worker_id):
                if kind == "prepare":
                    _process_prepare(store, task_id)
                elif kind == "solve":
                    _process_solve(store, cache, task_id, int(job["n"]))
                else:
                    raise ValueError(f"Неизвестный kind={kind}")
            store.ack_job(raw, job, "done")
        except Exception as exc:
            trace = traceback.format_exc()
            store.ack_job(raw, job, "error")
            if kind == "prepare":
                store.patch_meta(task_id, state="failed", error=str(exc))
                store.publish_event(task_id, "preparation_failed", {"error": str(exc), "traceback": trace})
            else:
                n = int(job["n"])
                store.set_n_status(task_id, n, "error", error=str(exc))
                store.publish_event(task_id, "n_error", {"n": n, "error": str(exc), "traceback": trace})
        finally:
            if kind == "solve":
                store.refill_task(task_id)
            try:
                store.refresh_task_state(task_id)
            except Exception:
                pass


def main() -> None:
    run_worker()


if __name__ == "__main__":
    main()
