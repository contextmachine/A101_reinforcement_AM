"""File-backed v2 artifacts: shared directory + per-pod cache + reference rows."""

from __future__ import annotations

import time

import pytest

from rebar_service.codec import sha256
from rebar_service.config import Settings
from rebar_service.v2.artifacts import FileArtifactBackend


def test_write_read_delete_round_trip_with_cache(tmp_path):
    backend = FileArtifactBackend(tmp_path / "shared", tmp_path / "cache")
    payload = b"x" * 10_000
    relative = backend.write("task1", "problem", payload)
    assert relative == "task1/problem.bin"
    assert (tmp_path / "shared" / relative).read_bytes() == payload
    digest = sha256(payload)
    cache = tmp_path / "cache" / digest[:2] / f"{digest}.bin"
    assert cache.exists()
    # a read served from the cache does not need the shared file any more
    (tmp_path / "shared" / relative).unlink()
    assert backend.read(relative, digest) == payload
    backend.delete(relative, digest)
    assert not cache.exists()


def test_read_waits_for_a_file_written_by_another_pod(tmp_path):
    backend = FileArtifactBackend(tmp_path / "shared", None, missing_retry_seconds=2.0, retry_interval_seconds=0.1)
    payload = b"late"
    digest = sha256(payload)
    import threading

    def writer():
        time.sleep(0.4)
        FileArtifactBackend(tmp_path / "shared", None).write("t", "k", payload)

    threading.Thread(target=writer).start()
    assert backend.read("t/k.bin", digest) == payload


def test_read_gives_up_on_missing_file_and_rejects_corruption(tmp_path):
    backend = FileArtifactBackend(tmp_path / "shared", None, missing_retry_seconds=0.2, retry_interval_seconds=0.05)
    with pytest.raises(FileNotFoundError):
        backend.read("nope/x.bin", "00" * 32)
    relative = backend.write("t", "k", b"good")
    (tmp_path / "shared" / relative).write_bytes(b"bad!")
    with pytest.raises(IOError):
        backend.read(relative, sha256(b"good"))


def test_keys_with_separators_map_to_safe_file_names(tmp_path):
    backend = FileArtifactBackend(tmp_path / "shared", None)
    assert backend.relative_path("abc", "solver:12") == "abc/solver_12.bin"
    assert backend.relative_path("../x", "fit:3") == "x/fit_3.bin"


def test_cache_purges_stale_entries(tmp_path):
    backend = FileArtifactBackend(tmp_path / "shared", tmp_path / "cache", cache_ttl_seconds=0.2)
    backend.write("t", "old", b"1")
    stale = next((tmp_path / "cache").rglob("*.bin"))
    assert stale.exists()
    time.sleep(0.4)
    backend.write("t", "new", b"2")  # purge runs on each write
    assert not stale.exists()


def test_settings_expose_artifact_paths_and_highs_overrides(tmp_path):
    s = Settings(artifact_dir="", highs_options="")
    assert s.artifact_path is None and s.highs_option_overrides == {}
    s = Settings(artifact_dir=str(tmp_path / "a"), artifact_cache_dir=str(tmp_path / "c"),
                 highs_options='{"presolve": "off", "mip_rel_gap": 0.01}')
    assert s.artifact_path == tmp_path / "a" and s.artifact_cache_path == tmp_path / "c"
    assert s.highs_option_overrides == {"presolve": "off", "mip_rel_gap": 0.01}
    with pytest.raises(ValueError):
        Settings(highs_options="[1]").highs_option_overrides
