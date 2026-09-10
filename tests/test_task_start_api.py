from __future__ import annotations

import pytest
from pydantic import ValidationError

from rebar_service.models import AnalysisTaskStart, TaskParameters


def base(**updates):
    payload = {
        "scene_id": "scene",
        "overlay_id": -1,
        "smooth": True,
        "n": [1, 2, 5],
        "components": [-2],
    }
    payload.update(updates)
    return payload


def test_analysis_component_special_selectors_are_exclusive():
    for value in ([-1], [-2], [-3], [0, 2, 5]):
        model = AnalysisTaskStart.model_validate(base(components=value))
        assert model.components == value
    for value in ([-1, 0], [-2, 5], [-3, 1], [-4]):
        with pytest.raises(ValidationError):
            AnalysisTaskStart.model_validate(base(components=value))


def test_analysis_n_is_explicit_positive_list():
    with pytest.raises(ValidationError):
        AnalysisTaskStart.model_validate(base(n=[0, 1]))
    assert AnalysisTaskStart.model_validate(base(n=[4, 8])).n == [4, 8]


def test_default_anchor_factor_is_40():
    assert TaskParameters.model_validate({"n": [1]}).anchor_factor == 40.0


def test_anchor_factor_default_is_40_across_geometry_pipeline():
    import inspect
    from A101.axis_orientation import add_box_anchorage, class_holds
    from A101.reinforcement_components import (
        fit_component_frontier,
        prepare_component_problem,
        split_reinforcement_components,
    )

    for fn in (
        class_holds,
        add_box_anchorage,
        split_reinforcement_components,
        prepare_component_problem,
        fit_component_frontier,
    ):
        assert inspect.signature(fn).parameters["anchor_factor"].default == 40.0


def test_analysis_task_rejects_n_above_solver_hard_cap():
    import pytest
    from pydantic import ValidationError
    from rebar_service.models import AnalysisTaskStart

    with pytest.raises(ValidationError):
        AnalysisTaskStart.model_validate({
            "scene_id": "scene",
            "n": [101],
            "components": [-2],
        })
