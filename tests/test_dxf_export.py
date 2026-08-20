from pathlib import Path

import ezdxf
import pytest

from rebar_service.dxf_export import DxfExportError, build_solution_dxf


def _source_dxf_bytes(tmp_path: Path) -> bytes:
    doc = ezdxf.new("R2018")
    doc.layers.add("ORIGINAL")
    doc.modelspace().add_line((0, 0), (10, 0), dxfattribs={"layer": "ORIGINAL"})
    path = tmp_path / "source.dxf"
    doc.saveas(path)
    return path.read_bytes()


def _result():
    return {
        "is_feasible": True,
        "fit_result": {
            "is_feasible": True,
            "zones": [
                {
                    "class": 3,
                    "bounds": (1000.0, 2000.0, 2000.0, 5000.0),
                }
            ],
        },
        "summary": {
            "N": 1,
            "mass without anchorage": 10.0,
            "mass with anchorage": 12.0,
            "anchorage mass": 2.0,
            "zones": [
                {
                    "primary rectangle": (900.0, 1900.0, 2100.0, 5100.0),
                    "final rectangle": (1000.0, 2000.0, 2000.0, 5000.0),
                    "final rectangle with anchorage": (1000.0, 1500.0, 2000.0, 5500.0),
                    "width": 1000.0,
                    "length": 3000.0,
                    "diameter": 25.0,
                    "step": 150.0,
                    "hold": 500.0,
                    "bars count": 2,
                    "zone mass without anchorage": 10.0,
                    "zone mass with anchorage": 12.0,
                    "anchorage mass": 2.0,
                    "bars": [
                        (1000.0, 2000.0, 1000.0, 5000.0),
                        (2000.0, 2000.0, 2000.0, 5000.0),
                    ],
                    "bars with anchorage": [
                        (1000.0, 1500.0, 1000.0, 5500.0),
                        (2000.0, 1500.0, 2000.0, 5500.0),
                    ],
                }
            ],
        },
    }


def test_export_preserves_source_and_adds_solution_layers(tmp_path):
    source = {
        "kind": "dxf",
        "filename": "Верхнее армирование.dxf",
        "content": _source_dxf_bytes(tmp_path),
    }

    exported = build_solution_dxf(source, _result(), n=30)
    path = tmp_path / exported.filename
    path.write_bytes(exported.content)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    assert len(msp.query('LINE[layer=="ORIGINAL"]')) == 1
    expected_layers = {
        "RB_C3_PRIMARY",
        "RB_C3_ZONE",
        "RB_C3_ZONE_ANCH",
        "RB_C3_BAR",
        "RB_C3_ANCH",
        "RB_LABEL",
    }
    assert expected_layers <= {layer.dxf.name for layer in doc.layers}

    bars = list(msp.query('LINE[layer=="RB_C3_BAR"]'))
    assert len(bars) == 2
    assert tuple(bars[0].dxf.start)[:2] == pytest.approx((1.0, 2.0))
    assert tuple(bars[0].dxf.end)[:2] == pytest.approx((1.0, 5.0))

    anch = list(msp.query('LINE[layer=="RB_C3_ANCH"]'))
    assert len(anch) == 4
    ys = sorted(
        round(float(v), 3)
        for e in anch
        for v in (e.dxf.start.y, e.dxf.end.y)
    )
    assert 1.5 in ys and 5.5 in ys

    labels = list(msp.query('TEXT[layer=="RB_LABEL"]'))
    assert len(labels) == 1
    assert "N=30" in labels[0].dxf.text
    assert "CLASS=3" in labels[0].dxf.text

    assert bars[0].has_xdata("REBAR_OPT")
    assert exported.filename.endswith("_N30_rebar.dxf")


def test_export_rejects_non_dxf_source():
    with pytest.raises(DxfExportError, match="DXF"):
        build_solution_dxf({"kind": "polygons"}, _result(), n=30)


def test_export_requires_postprocessed_solution(tmp_path):
    source = {
        "kind": "dxf",
        "filename": "source.dxf",
        "content": _source_dxf_bytes(tmp_path),
    }
    with pytest.raises(DxfExportError, match="postprocess"):
        build_solution_dxf(source, {"is_feasible": True}, n=30)
