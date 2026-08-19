from rebar_service.planner import coarse_refinement_order, normalize_n_request
from rebar_service.models import RangeN


def test_range_starts_coarse_then_half_offset():
    order = coarse_refinement_order(1, 100, 10)
    assert order[:10] == list(range(10, 101, 10))
    assert order[10:20] == list(range(5, 100, 10))
    assert sorted(order) == list(range(1, 101))


def test_n_normalization():
    mode, order, _ = normalize_n_request([20, 10, 20])
    assert mode == "list"
    assert order == [20, 10]
    mode, order, _ = normalize_n_request(RangeN(start=3, stop=5, coarse_step=2))
    assert mode == "range"
    assert sorted(order) == [3, 4, 5]


def test_validate_plan_limits_before_expansion():
    from rebar_service.planner import validate_n_request_limits

    validate_n_request_limits(RangeN(start=1, stop=100), max_values=100, max_n=100)
    try:
        validate_n_request_limits(RangeN(start=1, stop=101), max_values=100, max_n=1000)
    except ValueError as exc:
        assert "слишком много" in str(exc)
    else:
        raise AssertionError("range limit was not enforced")

    try:
        validate_n_request_limits([1, 999], max_values=10, max_n=100)
    except ValueError as exc:
        assert "максимальный" in str(exc)
    else:
        raise AssertionError("max N limit was not enforced")


def test_validate_solver_limits():
    from rebar_service.planner import validate_solver_limits

    validate_solver_limits({"threads": 4, "timeout_seconds": 120, "solver_time_limit": 100}, max_threads=4, max_timeout=300)
    for solver, message in [
        ({"threads": 5, "timeout_seconds": 120}, "threads"),
        ({"threads": 1, "timeout_seconds": 301}, "timeout"),
        ({"threads": 1, "timeout_seconds": 100, "solver_time_limit": 101}, "solver_time_limit"),
    ]:
        try:
            validate_solver_limits(solver, max_threads=4, max_timeout=300)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(f"limit was not enforced for {solver}")
