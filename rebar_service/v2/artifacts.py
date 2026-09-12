"""File-backed storage for large v2 artifacts (prepared problems, solver rows, fits).

Postgres holds only a small reference row per artifact; the bytes live in a shared directory
(the csi-s3 PVC mounted in every solver worker) and are mirrored into a per-pod cache so the
same prepared problem is downloaded once per worker instead of once per N.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from ..codec import sha256

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(key: str) -> str:
    cleaned = _SAFE.sub("_", str(key)).strip("._") or "artifact"
    return cleaned[:120]


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class FileArtifactBackend:
    """Shared-directory artifact bytes with a local read cache.

    ``root`` is the shared directory (visible to every worker); ``cache_root`` is a local
    directory (emptyDir) keyed by content hash. Reads retry for ``missing_retry_seconds`` so a
    file written by another pod through an S3 FUSE mount with a short metadata cache is found.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        cache_root: str | os.PathLike[str] | None = None,
        *,
        cache_ttl_seconds: float = 43200.0,
        missing_retry_seconds: float = 90.0,
        retry_interval_seconds: float = 3.0,
    ) -> None:
        self.root = Path(root)
        self.cache_root = Path(cache_root) if cache_root else None
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self.missing_retry_seconds = float(missing_retry_seconds)
        self.retry_interval_seconds = float(retry_interval_seconds)

    # ---------- paths ----------
    def relative_path(self, task_id: str, key: str) -> str:
        return f"{_safe_name(task_id)}/{_safe_name(key)}.bin"

    def shared_path(self, relative: str) -> Path:
        return self.root / relative

    def _cache_path(self, digest: str) -> Path | None:
        if self.cache_root is None:
            return None
        return self.cache_root / digest[:2] / f"{digest}.bin"

    # ---------- operations ----------
    def write(self, task_id: str, key: str, payload: bytes) -> str:
        relative = self.relative_path(task_id, key)
        _atomic_write(self.shared_path(relative), payload)
        digest = sha256(payload)
        cache = self._cache_path(digest)
        if cache is not None:
            try:
                _atomic_write(cache, payload)
            except OSError:
                pass  # the cache is an optimisation only
        self._purge_cache()
        return relative

    def read(self, relative: str, expected_sha256: str) -> bytes:
        cache = self._cache_path(expected_sha256)
        if cache is not None and cache.exists():
            payload = cache.read_bytes()
            if sha256(payload) == expected_sha256:
                try:
                    os.utime(cache, None)
                except OSError:
                    pass
                return payload
        path = self.shared_path(relative)
        deadline = time.monotonic() + self.missing_retry_seconds
        while True:
            try:
                payload = path.read_bytes()
                break
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self.retry_interval_seconds)
        if sha256(payload) != expected_sha256:
            raise IOError(f"артефакт {relative} повреждён")
        if cache is not None:
            try:
                _atomic_write(cache, payload)
            except OSError:
                pass
        return payload

    def delete(self, relative: str, digest: str | None = None) -> None:
        path = self.shared_path(relative)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        if digest:
            cache = self._cache_path(digest)
            if cache is not None:
                try:
                    cache.unlink()
                except FileNotFoundError:
                    pass

    def _purge_cache(self) -> None:
        if self.cache_root is None or not self.cache_root.exists():
            return
        cutoff = time.time() - self.cache_ttl_seconds
        try:
            for bucket in self.cache_root.iterdir():
                if not bucket.is_dir():
                    continue
                for entry in bucket.iterdir():
                    try:
                        if entry.stat().st_mtime < cutoff:
                            entry.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def drop_cache(self) -> None:
        if self.cache_root is not None:
            shutil.rmtree(self.cache_root, ignore_errors=True)
