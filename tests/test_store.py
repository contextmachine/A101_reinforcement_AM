from rebar_service.codec import decode_object, encode_object
from rebar_service.store import RedisStore


def test_codec_roundtrip_without_optional_zstd():
    value = {"payload": "x" * 200, "numbers": list(range(10))}
    payload, _ = encode_object(value)
    assert decode_object(payload) == value


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
    assert "lpush(self.names.workload, job_id)" in source
    assert "lrem(self.names.workload, 1, job_id)" in source
    assert "lpush(self.names.workload, raw)" not in source


def test_queue_names_default_to_the_main_queue():
    from rebar_service.config import Settings
    from rebar_service.redis_queue import QueueNames, RedisQueue

    settings = Settings()
    queue = RedisQueue(settings)
    assert queue.names == QueueNames(
        settings.ready_queue, settings.processing_queue, settings.workload_queue
    )
    assert queue.names.workload == "rebar:jobs:workload"


def test_isolated_queues_share_no_list_with_the_main_queue():
    from rebar_service.config import Settings
    from rebar_service.redis_queue import QueueNames, RedisQueue

    settings = Settings()
    main = RedisQueue(settings)
    bars = RedisQueue(settings, names=QueueNames.bars(settings))
    verification = RedisQueue(settings, names=QueueNames.verification(settings))

    assert bars.names == QueueNames("rebar:bars:ready", "rebar:bars:processing", "rebar:bars:workload")
    assert verification.names == QueueNames(
        "rebar:verification:ready", "rebar:verification:processing", "rebar:verification:workload"
    )
    triples = [
        {q.names.ready, q.names.processing, q.names.workload} for q in (main, bars, verification)
    ]
    assert not triples[0] & triples[1]
    assert not triples[0] & triples[2]
    assert not triples[1] & triples[2]
    # Each queue reaps only its own processing list, so the locks must differ too.
    locks = {main._reaper_lock, bars._reaper_lock, verification._reaper_lock}
    assert len(locks) == 3
    assert main._reaper_lock == "queue-reaper"
