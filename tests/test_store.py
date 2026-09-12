import pytest
from contextlib import contextmanager

from rebar_service.codec import decode_object, encode_object, sha256
from rebar_service.config import Settings
from rebar_service.postgres_store import PostgresStore
from rebar_service.store import RedisStore


def test_codec_roundtrip_without_optional_zstd():
    value = {"payload": "x" * 200, "numbers": list(range(10))}
    payload, _ = encode_object(value)
    assert decode_object(payload) == value

def test_codec_roundtrip_accepts_persisted_codec_metadata():
    value = {"stage": "prepared", "numbers": [1, 2, 3]}
    payload, codec = encode_object(value)
    assert decode_object(payload, codec) == value

def test_codec_rejects_mismatched_persisted_codec_metadata():
    value = {"stage": "prepared"}
    payload, codec = encode_object(value)
    wrong = "pickle+zlib" if codec == "pickle+zstd" else "pickle+zstd"
    with pytest.raises(ValueError, match="Codec mismatch"):
        decode_object(payload, wrong)



class _ArtifactResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _ArtifactConnection:
    def __init__(self, row):
        self.row = row

    def execute(self, *_args, **_kwargs):
        return _ArtifactResult(self.row)


class _ArtifactDatabase:
    def __init__(self, row):
        self.row = row

    @contextmanager
    def connect(self):
        yield _ArtifactConnection(self.row)


def test_v2_artifact_loader_roundtrips_payload_with_stored_codec():
    value = {"problem": {"n": 4}, "items": [1, 2, 3]}
    payload, codec = encode_object(value)
    database = _ArtifactDatabase({"codec": codec, "payload": payload, "sha256": sha256(payload)})
    store = PostgresStore(Settings(), database=database)

    assert store.load_v2_artifact("task", "prepared_problem") == value

def test_jsonutil_replaces_non_finite_numbers():
    from rebar_service.jsonutil import dumps

    text = dumps({"gap": float("nan"), "bound": float("inf")})
    assert text == '{"gap":null,"bound":null}'


def test_result_rank_prefers_postprocessed_feasible_over_raw_incumbent():
    raw = {"is_feasible": True, "is_optimal": False, "total_cost": 90.0, "kind": "incumbent", "postprocessed": False}
    final = {"is_feasible": True, "is_optimal": False, "total_cost": 100.0, "kind": "final", "postprocessed": True}
    broken = {"is_feasible": False, "is_optimal": False, "total_cost": 80.0, "kind": "final", "postprocessed": True}
    assert RedisStore._result_rank(final) < RedisStore._result_rank(raw)
    assert RedisStore._result_rank(raw) < RedisStore._result_rank(broken)


def test_store_tracks_total_outstanding_work_for_keda_by_job_id_only():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "rebar_service/redis_queue.py").read_text(encoding="utf-8")
    assert "lpush(self.settings.workload_queue, job_id)" in source
    assert "lrem(self.settings.workload_queue, 1, job_id)" in source
    assert "lpush(self.settings.workload_queue, raw)" not in source
