from __future__ import annotations

import pytest

import A101.calculate_mass as cm
from rebar_service.models import TaskParameters


def test_user_stock_removes_integer_multiple_steps_and_sets_max_layers():
    stock, max_layers = cm.normalize_rebar_stock(
        [(18, 400), (18, 300), (18, 200), (18, 150), (18, 100)]
    )

    assert stock == [(18, 400), (18, 300)]
    assert max_layers == 4


def test_user_stock_max_layers_is_global_across_diameters():
    stock, max_layers = cm.normalize_rebar_stock(
        [
            (18, 400), (18, 200), (18, 100),
            (20, 450), (20, 150),
            (25, 300), (25, 200),
        ]
    )

    assert stock == [(18, 400), (20, 450), (25, 300), (25, 200)]
    assert max_layers == 4


def test_resolve_rebar_config_uses_select_rebar_config_when_background_and_stock_omitted(monkeypatch):
    called = {}

    def fake_select(data, *, max_lay=2, strategy="min_background"):
        called["max_lay"] = max_lay
        called["strategy"] = strategy
        return {
            "load2cls": {1.0: 0, 2.0: 1},
            "recipes": {},
            "densities": {1: 10.0},
            "diameters": {1: 20},
            "steps": {1: 150},
            "back_arm": 9.0,
            "back_grid": (14, 300),
            "stock": [(20, 150), (20, 100)],
        }

    monkeypatch.setattr(cm, "select_rebar_config", fake_select)

    cfg = cm.resolve_rebar_config(
        [{"load": 1.0}, {"load": 2.0}],
        back_grid=None,
        stock=None,
        max_layers=None,
    )

    assert called == {"max_lay": 2, "strategy": "min_background"}
    assert cfg["back_grid"] == (14, 300)
    assert cfg["stock"] == [(20, 150), (20, 100)]
    assert cfg["max_layers"] == 2
    assert cfg["rebar_config_source"] == "catalog_auto"


def test_resolve_rebar_config_preserves_explicit_max_layers(monkeypatch):
    captured = {}

    def fake_make(loads, back_grid, stock, *, max_lay=2, **kwargs):
        captured["max_lay"] = max_lay
        return {
            "load2cls": {float(x): 1 for x in loads},
            "recipes": {},
            "densities": {1: 10.0},
            "diameters": {1: 16},
            "steps": {1: 300},
            "back_arm": 1.0,
        }

    monkeypatch.setattr(
        cm,
        "make_rebar_classes",
        fake_make,
    )

    cfg = cm.resolve_rebar_config(
        [{"load": 19.0}],
        back_grid=(16, 300),
        stock=[
            (16, 300),
            (20, 150),
            (20, 100),
        ],
        max_layers=2,
    )

    assert captured["max_lay"] == 2
    assert cfg["max_layers"] == 2


def test_two_16_300_layers_are_used_for_load_19():
    cfg = cm.resolve_rebar_config(
        [
            {"load": 5.7},
            {"load": 12.0},
            {"load": 19.0},
            {"load": 27.0},
            {"load": 37.0},
        ],
        back_grid=(16, 300),
        stock=[
            (16, 300),
            (20, 150),
            (20, 100),
        ],
        max_layers=2,
    )

    cls_19 = cfg["load2cls"][19.0]
    recipe = cfg["recipes"][cls_19]

    assert len(recipe) == 2

    assert [
        (cfg["diameters"][cls], cfg["steps"][cls])
        for cls in recipe
    ] == [
        (16, 300),
        (16, 300),
    ]



def test_resolve_rebar_config_rejects_stock_without_background():
    with pytest.raises(ValueError, match="back_grid"):
        cm.resolve_rebar_config(
            [{"load": 4.0}],
            back_grid=None,
            stock=[(18, 300)],
            max_layers=None,
        )


def test_task_parameters_allow_rebar_catalog_auto_selection():
    params = TaskParameters.model_validate({"n": [1, 2]})
    assert params.back_grid is None
    assert params.stock is None
    assert params.max_layers is None
