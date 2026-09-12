from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)


def solver_logging_enabled(settings: Settings) -> bool:
    return all(
        bool(str(value).strip()) if value is not None else False
        for value in (
            settings.solver_log_bucket,
            settings.solver_log_prefix,
            settings.solver_log_access_key,
            settings.solver_log_secret_key,
        )
    )


def _create_temp_log() -> str:
    fd, path = tempfile.mkstemp(prefix="rebar-highs-", suffix=".log")
    os.close(fd)
    return path


def _upload_solver_log(settings: Settings, path: str, object_key: str) -> None:
    import boto3

    client = boto3.client(
        "s3",
        aws_access_key_id=settings.solver_log_access_key,
        aws_secret_access_key=settings.solver_log_secret_key,
    )
    client.upload_file(str(path), str(settings.solver_log_bucket), str(object_key))


class SolverLogCapture:
    """Optional HiGHS file log whose upload never changes solver semantics."""

    def __init__(self, settings: Settings, *, task_id: str, n: int, attempt: int) -> None:
        self.settings = settings
        self.task_id = str(task_id)
        self.n = int(n)
        self.attempt = int(attempt)
        self.local_path: str | None = None
        self.object_key: str | None = None
        self.upload_error: str | None = None

    @property
    def enabled(self) -> bool:
        return solver_logging_enabled(self.settings)

    @property
    def highs_options(self) -> dict[str, Any]:
        return {} if self.local_path is None else {"log_file": self.local_path}

    def __enter__(self) -> "SolverLogCapture":
        if not self.enabled:
            return self
        self.local_path = _create_temp_log()
        prefix = str(self.settings.solver_log_prefix).strip().strip("/")
        leaf = f"{self.task_id}/n-{self.n}/attempt-{self.attempt}.log"
        self.object_key = f"{prefix}/{leaf}" if prefix else leaf
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        path = self.local_path
        if path is None:
            return False
        try:
            if Path(path).exists():
                try:
                    _upload_solver_log(self.settings, path, str(self.object_key))
                except Exception as upload_exc:  # logging must not invalidate a valid solve
                    self.upload_error = f"{type(upload_exc).__name__}: {upload_exc}"
                    logger.warning("Failed to upload HiGHS log for task=%s n=%s attempt=%s: %s",
                                   self.task_id, self.n, self.attempt, type(upload_exc).__name__)
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
        return False
