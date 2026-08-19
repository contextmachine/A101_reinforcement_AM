from __future__ import annotations

import json
import os
import queue
from contextlib import contextmanager
import signal
import subprocess
import tempfile
import time
import traceback
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Mapping

import numpy as np

try:  # package import
    from .select_min_density_rectangles_recipes import (
        _coerce_prepared_rectangle_problem,
        materialize_prepared_original_indices,
        select_min_density_rectangles,
    )
    from .rectangle_solver_job import (
        _best_warm_start_prepared,
        _cached_optimal_entry_prepared,
        _default_solver_time_limit,
        _ensure_job_result_metadata,
        _kill_process_tree,
        _mark_hard_timeout_fallback,
        _materialize_cached_infeasible,
        _normalize_data,
        _prefer_better_known_incumbent,
        _register_result,
        _timeout_result,
    )
except ImportError:  # direct module import
    from select_min_density_rectangles_recipes import (
        _coerce_prepared_rectangle_problem,
        materialize_prepared_original_indices,
        select_min_density_rectangles,
    )
    from rectangle_solver_job import (
        _best_warm_start_prepared,
        _cached_optimal_entry_prepared,
        _default_solver_time_limit,
        _ensure_job_result_metadata,
        _kill_process_tree,
        _mark_hard_timeout_fallback,
        _materialize_cached_infeasible,
        _normalize_data,
        _prefer_better_known_incumbent,
        _register_result,
        _timeout_result,
    )


STREAM_SCHEMA_VERSION = 1


def _put_latest(event_queue, event: Mapping[str, Any]) -> None:
    """Never block HiGHS; do not let a heartbeat evict an incumbent."""

    payload = dict(event)
    try:
        event_queue.put_nowait(payload)
        return
    except queue.Full:
        pass

    # Progress is expendable. In particular, never replace an unconsumed
    # improving solution by a heartbeat merely because the UI is slow.
    if payload.get("type") == "heartbeat":
        return

    # A newer incumbent/final/error may replace one stale queue item. The
    # final result itself is additionally carried by a dedicated Pipe.
    try:
        event_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        event_queue.put_nowait(payload)
    except queue.Full:
        pass


def _stream_result_from_indices(
    prepared: Mapping[str, Any],
    original_indices: list[int],
    N: int,
    *,
    elapsed: float,
    gap: float | None,
    nodes: int | None,
    best_bound: float | None,
) -> dict[str, Any]:
    result = materialize_prepared_original_indices(
        prepared,
        original_indices,
        n_requested=N,
        status="Feasible",
        is_optimal=False,
    )
    result.update(
        {
            "status": "Feasible",
            "is_feasible": True,
            "is_optimal": False,
            "solver_proved_optimal": False,
            "infeasibility_proved": False,
            "termination_reason": "searching",
            "time_limit_reached": False,
            "has_incumbent": True,
            "incumbent_source": "highs_callback",
            "incumbent_is_solver_best": True,
            "solver_incumbent_recovered": True,
        }
    )
    result["stats"].update(
        {
            "backend": "highs",
            "solver_class": "highspy.Highs",
            "streamed_incumbent": True,
            "elapsed": float(elapsed),
            "mip_gap": gap,
            "mip_node_count": nodes,
            "best_bound": best_bound,
        }
    )
    return result


def _stream_worker(
    event_queue,
    final_conn,
    call: dict[str, Any],
    N: int,
    emit_interval: float,
    emit_every_nodes: int | None,
    emit_on_improvement: bool,
    emit_heartbeat: bool,
) -> None:
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass

    try:
        prepared = _coerce_prepared_rectangle_problem(call["prepared"])
        sequence = 0
        last_emit = -float("inf")
        last_heartbeat = -float("inf")
        last_heartbeat_nodes = -1
        best_cost = float("inf")
        has_incumbent = False
        pending_event: dict[str, Any] | None = None

        def emit_incumbent(raw: Mapping[str, Any], *, force: bool = False) -> None:
            nonlocal sequence, last_emit, best_cost, has_incumbent, pending_event
            counts = np.asarray(raw["chosen_counts"], dtype=np.int32)
            elapsed = float(raw.get("elapsed", 0.0))
            cost = float(raw.get("total_cost", float("inf")))
            tolerance = 1e-9 * max(
                1.0, abs(cost), abs(best_cost) if np.isfinite(best_cost) else 1.0
            )
            if not np.isfinite(cost) or cost >= best_cost - tolerance:
                return

            local = np.repeat(
                np.flatnonzero(counts),
                counts[np.flatnonzero(counts)],
            ).astype(np.int32, copy=False)
            originals = np.asarray(
                prepared["candidate_original_indices"], dtype=np.int64
            )
            original_indices = [int(value) for value in originals[local]]

            best_cost = cost
            has_incumbent = True
            sequence += 1
            event = {
                "type": "incumbent",
                "schema_version": STREAM_SCHEMA_VERSION,
                "sequence": sequence,
                "N": int(N),
                "problem_id": str(prepared["problem_id"]),
                "elapsed": elapsed,
                "total_cost": cost,
                "gap": raw.get("gap"),
                "nodes": raw.get("nodes"),
                "best_bound": raw.get("best_bound"),
                # Keep the callback lightweight: the full public result is
                # materialized in the parent process when poll() consumes it.
                "indices": original_indices,
            }
            pending_event = event
            if force or emit_on_improvement or elapsed - last_emit >= emit_interval:
                _put_latest(event_queue, event)
                pending_event = None
                last_emit = elapsed

        def progress(raw: dict[str, Any]) -> None:
            nonlocal last_emit, last_heartbeat, last_heartbeat_nodes, pending_event
            kind = raw.get("kind")
            elapsed = float(raw.get("elapsed", 0.0))
            if kind == "incumbent":
                emit_incumbent(raw, force=(not has_incumbent))
                return

            if pending_event is not None and elapsed - last_emit >= emit_interval:
                _put_latest(event_queue, pending_event)
                pending_event = None
                last_emit = elapsed

            nodes = int(raw.get("nodes", 0) or 0)
            time_due = elapsed - last_heartbeat >= emit_interval
            nodes_due = (
                emit_every_nodes is not None
                and nodes >= 0
                and (last_heartbeat_nodes < 0 or nodes - last_heartbeat_nodes >= emit_every_nodes)
            )
            if emit_heartbeat and (time_due or nodes_due):
                heartbeat = {
                    "type": "heartbeat",
                    "schema_version": STREAM_SCHEMA_VERSION,
                    "N": int(N),
                    "problem_id": str(prepared["problem_id"]),
                    "elapsed": elapsed,
                    "best_incumbent_cost": (None if not has_incumbent else float(best_cost)),
                    "gap": raw.get("gap"),
                    "nodes": nodes,
                    "best_bound": raw.get("best_bound"),
                }
                _put_latest(event_queue, heartbeat)
                last_heartbeat = elapsed
                last_heartbeat_nodes = nodes

        progress.include_logging = bool(emit_heartbeat)
        solve_call = dict(call)
        solve_call.pop("prepared", None)
        solve_call.update(
            {
                "prepared": prepared,
                "n": int(N),
                "backend": "highs",
                "pulp_solver": "highs",
                "progress_callback": progress,
                # Preserve an incumbent even when the soft limit is reached.
                "require_optimal": False,
            }
        )
        result = select_min_density_rectangles(**solve_call)

        if pending_event is not None:
            _put_latest(event_queue, pending_event)
        final_conn.send(("ok", result))
        _put_latest(
            event_queue,
            {
                "type": "final",
                "schema_version": STREAM_SCHEMA_VERSION,
                "N": int(N),
                "problem_id": str(prepared["problem_id"]),
                "result": result,
            },
        )
    except BaseException:
        error = traceback.format_exc()
        try:
            final_conn.send(("error", error))
        except (BrokenPipeError, EOFError, OSError):
            pass
        _put_latest(event_queue, {"type": "error", "N": int(N), "error": error})
    finally:
        final_conn.close()


class RectangleSolverJob:
    """Killable direct-HiGHS job that streams improving integer solutions."""

    def __init__(
        self,
        *,
        process,
        event_queue,
        final_conn,
        prepared: Mapping[str, Any],
        data: dict[str, Any],
        N: int,
        timeout: float,
        solver_time_limit: float,
        warm_indices: list[int] | None,
        warm_from_N: int | None,
        require_optimal: bool,
        return_best_on_timeout: bool,
        raise_worker_errors: bool,
        immediate_result: dict[str, Any] | None = None,
    ) -> None:
        self._process = process
        self._queue = event_queue
        self._conn = final_conn
        self._prepared = prepared
        self._data = data
        self._N = int(N)
        self._timeout = float(timeout)
        self._solver_time_limit = float(solver_time_limit)
        self._warm_indices = warm_indices
        self._warm_from_N = warm_from_N
        self._require_optimal = bool(require_optimal)
        self._return_best_on_timeout = bool(return_best_on_timeout)
        self._raise_worker_errors = bool(raise_worker_errors)
        self._started = time.monotonic()
        self._final_result = immediate_result
        self._final_error: str | None = None
        self._best_streamed: dict[str, Any] | None = None
        self._closed = immediate_result is not None

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def problem_id(self) -> str:
        return str(self._prepared["problem_id"])

    @property
    def N(self) -> int:
        return self._N

    @property
    def best_result(self) -> dict[str, Any] | None:
        return self._best_streamed or self._final_result

    def _elapsed(self) -> float:
        return float(time.monotonic() - self._started)

    def _record_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        event = dict(event)
        if event.get("type") == "incumbent":
            if isinstance(event.get("result"), Mapping):
                result = dict(event["result"])
            else:
                result = _stream_result_from_indices(
                    self._prepared,
                    [int(value) for value in event.get("indices", [])],
                    self._N,
                    elapsed=float(event.get("elapsed", 0.0)),
                    gap=event.get("gap"),
                    nodes=event.get("nodes"),
                    best_bound=event.get("best_bound"),
                )
                event["result"] = result
            if self._best_streamed is None or float(result["total_cost"]) < float(
                self._best_streamed["total_cost"]
            ):
                self._best_streamed = result
                _register_result(self._data, result, self._N)
        return event

    def _receive_final(self) -> bool:
        if self._final_result is not None or self._final_error is not None or self._conn is None:
            return self._final_result is not None or self._final_error is not None
        if not self._conn.poll(0):
            return False
        try:
            status, payload = self._conn.recv()
        except EOFError:
            status, payload = "error", "worker завершился без финального сообщения"
        if status == "ok":
            result = _ensure_job_result_metadata(dict(payload), incumbent_source="solver")
            if self._best_streamed is not None:
                result = _prefer_better_known_incumbent(result, dict(self._best_streamed))
            if self._warm_indices is not None:
                known = materialize_prepared_original_indices(
                    self._prepared,
                    self._warm_indices,
                    n_requested=self._N,
                    status="Feasible",
                    is_optimal=False,
                )
                result = _prefer_better_known_incumbent(result, known)
            stats = result.setdefault("stats", {})
            stats.update(
                {
                    "streaming_job": True,
                    "worker_elapsed_seconds": self._elapsed(),
                    "hard_timeout": self._timeout,
                    "solver_time_limit": self._solver_time_limit,
                    "warm_start_from_N": self._warm_from_N,
                    "require_optimal_requested": self._require_optimal,
                    "meets_requested_optimality": bool(
                        not self._require_optimal
                        or result.get("is_optimal")
                        or result.get("infeasibility_proved")
                    ),
                }
            )
            self._final_result = result
            _register_result(self._data, result, self._N)
        else:
            self._final_error = str(payload)
        return True

    def _drain_pending_events(self) -> None:
        if self._queue is None:
            return
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return
            self._record_event(event)

    def _hard_timeout(self) -> None:
        if self._closed:
            return
        self._drain_pending_events()
        if self._process is not None and self._process.is_alive():
            _kill_process_tree(self._process)
        self._drain_pending_events()

        if self._best_streamed is not None:
            result = dict(self._best_streamed)
            result.update(
                {
                    "status": "Feasible",
                    "is_optimal": False,
                    "solver_proved_optimal": False,
                    "termination_reason": "hard_timeout",
                    "time_limit_reached": True,
                    "incumbent_source": "streamed_incumbent",
                    "solver_incumbent_recovered": True,
                    "incumbent_is_solver_best": True,
                    "reason": "Worker остановлен; возвращён последний переданный HiGHS incumbent",
                }
            )
            result["stats"].update(
                {
                    "hard_timeout": self._elapsed(),
                    "solver_time_limit": self._solver_time_limit,
                    "streaming_job": True,
                    "termination_reason": "hard_timeout",
                    "time_limit_reached": True,
                    "warm_start_from_N": self._warm_from_N,
                }
            )
            self._final_result = result
            _register_result(self._data, result, self._N)
        elif self._warm_indices is not None:
            result = materialize_prepared_original_indices(
                self._prepared,
                self._warm_indices,
                n_requested=self._N,
                status="Feasible",
                is_optimal=False,
            )
            self._final_result = _mark_hard_timeout_fallback(
                result,
                elapsed=self._elapsed(),
                solver_time_limit=self._solver_time_limit,
                warm_from_N=self._warm_from_N,
                prepared_used=True,
                require_optimal=self._require_optimal,
            )
            _register_result(self._data, self._final_result, self._N)
        elif self._return_best_on_timeout:
            self._final_result = _timeout_result(
                N=self._N,
                problem_id=str(self._prepared["problem_id"]),
                elapsed=self._elapsed(),
                solver_time_limit=self._solver_time_limit,
                warm_from_N=self._warm_from_N,
                require_optimal=self._require_optimal,
                prepared_stats={**dict(self._prepared.get("stats", {})), "prepared_used": True},
            )
        self._closed = True

    def _check_state(self) -> None:
        if self._closed:
            return
        self._receive_final()
        if self._final_result is not None or self._final_error is not None:
            if self._process is not None:
                self._process.join(timeout=1)
            self._closed = True
            return
        if self._elapsed() >= self._timeout:
            self._hard_timeout()
            return
        if self._process is not None and not self._process.is_alive():
            self._receive_final()
            if self._final_result is None and self._final_error is None:
                self._final_error = f"worker exited with code {self._process.exitcode}"
            self._closed = True

    def is_alive(self) -> bool:
        self._check_state()
        return not self._closed

    def poll(self, timeout: float = 0.0) -> dict[str, Any] | None:
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        while True:
            self._check_state()
            remaining = max(0.0, deadline - time.monotonic())
            try:
                event = self._queue.get(timeout=min(0.10, remaining)) if self._queue is not None else None
            except queue.Empty:
                event = None
            if event is not None:
                return self._record_event(event)
            if self._closed or remaining <= 0:
                return None

    def cancel(self) -> None:
        if self._closed:
            return
        if self._process is not None and self._process.is_alive():
            _kill_process_tree(self._process)
        self._closed = True

    def result(self, timeout: float | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while not self._closed:
            wait = 0.10
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("RectangleSolverJob.result() превысил timeout ожидания")
                wait = min(wait, remaining)
            self.poll(wait)

        if self._final_error is not None:
            if self._raise_worker_errors:
                raise RuntimeError(f"Ошибка streaming worker при N={self._N}:\n{self._final_error}")
            return None, self._data
        return self._final_result, self._data

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._queue is not None:
            try:
                self._queue.close()
            except (AttributeError, OSError):
                pass

    def __enter__(self) -> "RectangleSolverJob":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.cancel()
        self.close()


def start_rectangle_job(
    *,
    prepared,
    data: Mapping[str, Any] | None,
    N: int,
    timeout: float = 300.0,
    solver_time_limit: float | None = None,
    threads: int = 1,
    solver_msg: bool = False,
    require_optimal: bool = True,
    use_warm_start: bool = True,
    cross_n_warm_start: bool = False,
    return_best_on_timeout: bool = True,
    emit_interval: float = 5.0,
    emit_every_nodes: int | None = None,
    emit_on_improvement: bool = True,
    emit_heartbeat: bool = False,
    queue_size: int = 16,
    highs_options: Mapping[str, Any] | None = None,
    raise_worker_errors: bool = True,
) -> RectangleSolverJob:
    """Start one prepared problem through direct highspy callbacks.

    Use a prepared file path on Windows and cluster nodes to avoid transferring
    the large N-independent model through multiprocessing spawn.
    """

    if isinstance(N, bool) or int(N) != N or int(N) < 0:
        raise ValueError("N должен быть неотрицательным целым")
    N = int(N)
    timeout = float(timeout)
    if not np.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout должен быть положительным")
    if isinstance(threads, bool) or int(threads) != threads or int(threads) <= 0:
        raise ValueError("threads должен быть положительным целым")
    threads = int(threads)
    emit_interval = float(emit_interval)
    if not np.isfinite(emit_interval) or emit_interval <= 0:
        raise ValueError("emit_interval должен быть положительным")
    if emit_every_nodes is not None:
        if (
            isinstance(emit_every_nodes, bool)
            or int(emit_every_nodes) != emit_every_nodes
            or int(emit_every_nodes) <= 0
        ):
            raise ValueError("emit_every_nodes должен быть положительным целым или None")
        emit_every_nodes = int(emit_every_nodes)
    if isinstance(queue_size, bool) or int(queue_size) != queue_size or int(queue_size) <= 0:
        raise ValueError("queue_size должен быть положительным целым")

    prepared_model = _coerce_prepared_rectangle_problem(prepared)
    max_n = prepared_model.get("max_n")
    if max_n is not None and N > int(max_n):
        raise ValueError(f"prepared рассчитан для max_n={max_n}, но запрошено N={N}")
    problem_id = str(prepared_model["problem_id"])
    updated_data = _normalize_data(data, problem_id)

    if N in set(map(int, updated_data.get("infeasible", []))):
        result = _materialize_cached_infeasible({}, N, problem_id)
        result = _ensure_job_result_metadata(result, incumbent_source=None)
        result["stats"].update({"prepared_used": True, "cache_hit": True, "streaming_job": True})
        return RectangleSolverJob(
            process=None,
            event_queue=None,
            final_conn=None,
            prepared=prepared_model,
            data=updated_data,
            N=N,
            timeout=timeout,
            solver_time_limit=0.0,
            warm_indices=None,
            warm_from_N=None,
            require_optimal=require_optimal,
            return_best_on_timeout=return_best_on_timeout,
            raise_worker_errors=raise_worker_errors,
            immediate_result=result,
        )

    cached = _cached_optimal_entry_prepared(updated_data, N, prepared_model)
    if cached is not None:
        result = materialize_prepared_original_indices(
            prepared_model,
            cached["indices"],
            n_requested=N,
            status="Optimal",
            is_optimal=True,
        )
        result = _ensure_job_result_metadata(result, incumbent_source="cache")
        result["stats"].update({"prepared_used": True, "cache_hit": True, "streaming_job": True})
        return RectangleSolverJob(
            process=None,
            event_queue=None,
            final_conn=None,
            prepared=prepared_model,
            data=updated_data,
            N=N,
            timeout=timeout,
            solver_time_limit=0.0,
            warm_indices=None,
            warm_from_N=None,
            require_optimal=require_optimal,
            return_best_on_timeout=return_best_on_timeout,
            raise_worker_errors=raise_worker_errors,
            immediate_result=result,
        )

    if use_warm_start:
        warm_indices, warm_from_N, _warm_cost = _best_warm_start_prepared(
            updated_data, N, prepared_model, allow_cross_n=bool(cross_n_warm_start)
        )
    else:
        warm_indices = warm_from_N = None

    if solver_time_limit is None:
        solver_time_limit = _default_solver_time_limit(timeout)
    else:
        solver_time_limit = min(float(solver_time_limit), _default_solver_time_limit(timeout))
        if not np.isfinite(solver_time_limit) or solver_time_limit <= 0:
            raise ValueError("solver_time_limit должен быть положительным")

    options = dict(highs_options or {})
    if emit_heartbeat:
        options.setdefault("mip_min_logging_interval", min(emit_interval, 5.0))
    call = {
        "prepared": prepared,
        "initial_indices": warm_indices,
        "solver_msg": bool(solver_msg),
        "time_limit": float(solver_time_limit),
        "threads": threads,
        "highs_options": options,
    }

    ctx = get_context("spawn")
    event_queue = ctx.Queue(maxsize=int(queue_size))
    parent_conn, child_conn = ctx.Pipe(False)
    process = ctx.Process(
        target=_stream_worker,
        args=(
            event_queue,
            child_conn,
            call,
            N,
            emit_interval,
            emit_every_nodes,
            bool(emit_on_improvement),
            bool(emit_heartbeat),
        ),
    )
    process.start()
    child_conn.close()
    return RectangleSolverJob(
        process=process,
        event_queue=event_queue,
        final_conn=parent_conn,
        prepared=prepared_model,
        data=updated_data,
        N=N,
        timeout=timeout,
        solver_time_limit=float(solver_time_limit),
        warm_indices=warm_indices,
        warm_from_N=warm_from_N,
        require_optimal=require_optimal,
        return_best_on_timeout=return_best_on_timeout,
        raise_worker_errors=raise_worker_errors,
    )


@contextmanager
def _exclusive_json_lock(
    target: Path,
    *,
    timeout: float = 30.0,
    stale_after: float = 300.0,
):
    """Cross-process lock based on atomic directory creation.

    This works on local filesystems and on the shared filesystems normally used
    for cluster jobs. Redis remains the stronger choice for distributed CAS.
    """

    lock_path = target.with_name(target.name + ".lock")
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            os.mkdir(lock_path)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > float(stale_after):
                    os.rmdir(lock_path)
                    continue
            except (FileNotFoundError, OSError):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Не удалось получить lock для {target}")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.rmdir(lock_path)
        except FileNotFoundError:
            pass


def _compact_incumbent_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    if event.get("type") != "incumbent":
        raise ValueError("Ожидается event типа 'incumbent'")
    result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
    if not result and not event.get("indices"):
        raise ValueError("incumbent event не содержит indices/result")
    return {
        "schema_version": STREAM_SCHEMA_VERSION,
        "type": "incumbent",
        "problem_id": event.get("problem_id"),
        "N": int(event["N"]),
        "sequence": int(event.get("sequence", 0)),
        "elapsed": float(event.get("elapsed", 0.0)),
        "total_cost": float(event["total_cost"]),
        "gap": event.get("gap"),
        "nodes": event.get("nodes"),
        "best_bound": event.get("best_bound"),
        "indices": [
            int(value)
            for value in (result.get("indices", []) or event.get("indices", []))
        ],
        "saved_at": time.time(),
    }


def save_incumbent_json(
    event: Mapping[str, Any],
    path: str | os.PathLike,
    *,
    lock_timeout: float = 30.0,
    stale_lock_after: float = 300.0,
) -> bool:
    """Atomically keep the cheapest JSON incumbent across processes.

    The compare-and-replace section is protected by an atomic lock directory,
    so concurrent jobs cannot let a worse solution overwrite a better one.
    """

    payload = _compact_incumbent_payload(event)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with _exclusive_json_lock(
        target, timeout=lock_timeout, stale_after=stale_lock_after
    ):
        if target.exists():
            try:
                current = json.loads(target.read_text(encoding="utf-8"))
                same_problem = current.get("problem_id") == payload.get("problem_id")
                same_n = int(current.get("N")) == int(payload["N"])
                if (
                    same_problem
                    and same_n
                    and float(current.get("total_cost", float("inf")))
                    <= float(payload["total_cost"])
                ):
                    return False
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

        fd, temporary = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return True


_REDIS_BEST_LUA = r"""
local raw = redis.call('GET', KEYS[1])
local candidate_cost = tonumber(ARGV[2])
if raw then
  local ok, current = pcall(cjson.decode, raw)
  if ok and current['total_cost'] and tonumber(current['total_cost']) <= candidate_cost then
    return 0
  end
end
redis.call('SET', KEYS[1], ARGV[1])
local ttl = tonumber(ARGV[3])
if ttl and ttl > 0 then redis.call('EXPIRE', KEYS[1], ttl) end
return 1
"""


def save_incumbent_redis(
    redis_client,
    key: str,
    event: Mapping[str, Any],
    *,
    ttl: int | None = None,
) -> bool:
    """Atomically keep the cheapest incumbent across concurrent cluster jobs."""

    payload = _compact_incumbent_payload(event)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    result = redis_client.eval(
        _REDIS_BEST_LUA,
        1,
        str(key),
        serialized,
        repr(float(payload["total_cost"])),
        str(0 if ttl is None else int(ttl)),
    )
    return bool(int(result))


__all__ = [
    "RectangleSolverJob",
    "start_rectangle_job",
    "save_incumbent_json",
    "save_incumbent_redis",
]
