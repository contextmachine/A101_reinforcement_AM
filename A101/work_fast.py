from __future__ import annotations

import os
import signal
import subprocess
import time
import traceback
from collections import deque
from multiprocessing import get_all_start_methods, get_context
from multiprocessing.connection import wait

from tqdm.auto import tqdm

from time import perf_counter
from contextlib import contextmanager


@contextmanager
def timer(name=""):
    start = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter() - start
        print(f"{name}: {elapsed:.6f} сек")

def _worker(conn, kwargs, n):
    if os.name == "posix":
        os.setsid()  # CBC попадёт в ту же отдельную process group
    try:
        from A101.select_min_density_rectangles2_fast import select_min_density_rectangles

        conn.send(("ok", select_min_density_rectangles(**kwargs, n=n)))
    except BaseException:
        conn.send(("error", traceback.format_exc()))
    finally:
        conn.close()


def _kill_tree(process):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
    except (ProcessLookupError, OSError):
        pass
    process.join()


def run_many_with_timeout(kwargs, ns, *, workers=16, timeout=110):
    ns = list(ns)
    pending, results = deque(ns), {}
    method = "fork" if "fork" in get_all_start_methods() else "spawn"
    ctx, active = get_context(method), {}

    with tqdm(total=len(ns)) as bar:
        try:
            while pending or active:
                while pending and len(active) < workers:
                    n = pending.popleft()
                    parent, child = ctx.Pipe(False)
                    process = ctx.Process(target=_worker, args=(child, kwargs, n))
                    process.start()
                    child.close()
                    active[parent] = (n, process, time.monotonic())

                for conn in wait(list(active), timeout=0.05):
                    n, process, _ = active.pop(conn)
                    try:
                        status, payload = conn.recv()
                    except EOFError:
                        status, payload = "error", f"worker exit code: {process.exitcode}"
                    conn.close()
                    process.join()
                    results[n] = payload if status == "ok" else None
                    if status != "ok":
                        print(f"\nn={n}:\n{payload}")
                    bar.update()

                now = time.monotonic()
                for conn, (n, process, started) in list(active.items()):
                    if now - started >= timeout:
                        active.pop(conn)
                        conn.close()
                        _kill_tree(process)
                        results[n] = None
                        print(f"\nn={n}: HARD TIMEOUT")
                        bar.update()
        finally:
            for conn, (_, process, _) in active.items():
                conn.close()
                _kill_tree(process)

    return [results[n] for n in ns]
