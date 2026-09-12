"""HiGHS log placement for the reinforcement-selection workers.

Every solver run of a task writes ``{solver_log_dir}/{task_id}/{n}/{worker-id}.log``.
The directory is created here (never inside the A101 solver modules) and the
resulting HiGHS options are applied by
``A101.select_min_density_rectangles_recipes._solve_prepared_with_highs`` and
``A101.fit_box_layout.fit_box_layout`` *after* their own defaults, so the file
log is written even when console output stays disabled.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(value: Any, fallback: str) -> str:
    """Return ``value`` as one path segment (no separators, no ``..``)."""

    text = _UNSAFE.sub("_", str(value if value is not None else "").strip())
    text = text.strip("._")
    return text or fallback


def solver_log_file(settings: Any, task_id: str, n: int, worker_id: str) -> str:
    """Return ``{log_dir}/{task_id}/{n}/{worker_id}.log`` and create its directory.

    ``settings`` is a :class:`rebar_service.config.Settings` (``solver_log_path``
    resolves ``REBAR_SOLVER_LOG_DIR`` or ``<app-directory>/logs``). Any object
    exposing ``solver_log_path``/``solver_log_dir`` works; a plain path-like
    value is accepted as the directory itself.
    """

    root = getattr(settings, "solver_log_path", None)
    if root is None:
        raw = getattr(settings, "solver_log_dir", settings)
        root = Path(str(raw)) if raw not in (None, "") else Path("logs")
    directory = Path(root) / _safe_segment(task_id, "task") / _safe_segment(int(n), "n")
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{_safe_segment(worker_id, 'worker')}.log")


def highs_log_options(log_file: str) -> dict[str, Any]:
    """HiGHS options that send the solver log to ``log_file`` only.

    HiGHS writes ``log_file`` only while ``output_flag`` is true; console output
    is switched off separately via ``log_to_console``.
    """

    return {"log_file": str(log_file), "output_flag": True, "log_to_console": False}
