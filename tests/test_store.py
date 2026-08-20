from rebar_service.codec import decode_object, encode_object
from rebar_service.store import RedisStore


def test_codec_roundtrip_without_optional_zstd():
    value = {"payload": "x" * 200, "numbers": list(range(10))}
    payload, _ = encode_object(value)
    assert decode_object(payload) == value


def test_solver_data_merge_keeps_best_and_optimal():
    a = {"problem_id": "p", "solutions": {"10": {"indices": [1], "cost": 9.0}}, "infeasible": []}
    b = {"problem_id": "p", "solutions": {"10": {"indices": [2], "cost": 10.0, "optimal": True}}, "infeasible": [11]}
    merged = RedisStore.merge_data_values(a, b)
    assert merged["solutions"]["10"]["optimal"] is True
    assert merged["solutions"]["10"]["indices"] == [2]
    assert merged["infeasible"] == [11]


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


def test_store_tracks_total_outstanding_work_for_keda():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "rebar_service/store.py").read_text(encoding="utf-8")
    assert "lpush(self.settings.workload_queue, raw)" in source
    assert "lrem(self.settings.workload_queue, 1, raw)" in source
