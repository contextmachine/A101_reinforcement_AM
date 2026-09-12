"""``prepare_component_problem(max_n_resolver=...)`` contract."""

from __future__ import annotations

import numpy as np
import pytest

from support.tiny_component_problem import tiny_problem


def test_resolver_receives_work_matrix_and_context_and_sets_max_n():
    seen = {}

    def resolver(work_matrix, context):
        seen["matrix"] = work_matrix
        seen["context"] = context
        return 3

    problem, cfg, _ = tiny_problem(max_n=99, max_n_resolver=resolver)
    matrix = seen["matrix"]
    assert isinstance(matrix, np.ndarray) and matrix.ndim == 2
    assert np.issubdtype(matrix.dtype, np.integer)
    assert np.array_equal(matrix, problem["work_matrix"])
    context = seen["context"]
    assert set(context) >= {"recipes", "work_x_edges", "work_y_edges", "holds", "component_id"}
    assert context["component_id"] == problem["component_id"]
    assert np.array_equal(context["work_x_edges"], problem["work_x_edges"])
    assert np.array_equal(context["work_y_edges"], problem["work_y_edges"])
    assert context["holds"] == problem["holds"]
    assert context["recipes"] == {int(k): tuple(v) for k, v in (cfg.get("recipes") or {}).items()}
    # The resolver's value replaces the explicit max_n.
    assert problem["max_n"] == 3
    assert problem["prepared"]["max_n"] == 3
    assert "max_n_resolver" in problem["stats"]["prepare_times_s"]


def test_resolver_none_means_no_cap_and_explicit_max_n_is_reported():
    problem, _, _ = tiny_problem(max_n=5, max_n_resolver=lambda m, c: None)
    assert problem["max_n"] is None
    assert problem["prepared"].get("max_n") is None

    problem, _, _ = tiny_problem(max_n=5)
    assert problem["max_n"] == 5
    assert problem["prepared"]["max_n"] == 5


def test_resolver_can_use_estimate_max_useful_n_without_cap():
    from A101.max_n_milp import estimate_max_useful_n

    calls = []

    def resolver(work_matrix, context):
        result = estimate_max_useful_n(work_matrix, recipes=context["recipes"])
        calls.append(result)
        return result["max_useful_n"]

    problem, _, _ = tiny_problem(max_n=None, max_n_resolver=resolver)
    assert calls and calls[0]["feasible"] and calls[0]["capped"] is False
    assert problem["max_n"] == calls[0]["max_useful_n"] >= 1


def test_resolver_rejects_invalid_values():
    with pytest.raises(ValueError):
        tiny_problem(max_n_resolver=lambda m, c: -1)
    with pytest.raises(ValueError):
        tiny_problem(max_n_resolver=lambda m, c: 2.5)
