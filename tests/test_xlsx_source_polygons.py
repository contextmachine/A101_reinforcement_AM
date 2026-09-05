from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import Workbook

import rebar_service.source_polygons as source_polygons


def _xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def _sample_tables(*, second_load_row: float = 999.0):
    nodes = _xlsx(
        [
            ["Таблица узлов"],
            [None, "Координаты"],
            ["№ узла", "X\n(м)", "Y\n(м)", "Z\n(м)"],
            [1, 0.0, 0.0, -8.97],
            [2, 1.0, 0.0, -8.97],
            [3, 1.0, 2.0, -8.97],
            [4, 0.0, 2.0, -8.97],
        ]
    )
    elements = _xlsx(
        [
            ["Таблица элементов"],
            [None],
            ["№ элем", "Тип элем", "№№ узлов"],
            [10, 44, "1,2,3,4"],
            [11, 42, "1,3,4"],
        ]
    )
    loads = _xlsx(
        [
            ["ГР", "Элемент", "AS1", "AS2", "AS3", "AS4", "ASW1", "ASW2"],
            [1, 10, 7.2, 8.1, 9.2, 10.3, 1.0, "---"],
            [1, 10, second_load_row, second_load_row, second_load_row, second_load_row],
            [1, 11, 11.1, 12.2, 13.3, 14.4, 2.0, "---"],
            [1, 11, second_load_row, second_load_row, second_load_row, second_load_row],
        ]
    )
    return nodes, elements, loads


def _parser():
    parser = getattr(source_polygons, "source_polygons_from_xlsx", None)
    assert callable(parser), "source_polygons_from_xlsx() must exist"
    return parser


def test_xlsx_parser_maps_load_column_and_converts_meters_to_millimeters():
    nodes, elements, loads = _sample_tables()

    payload = _parser()(nodes, elements, loads, 2)

    assert payload["kind"] == "polygons"
    assert payload["units"] == "mm"
    assert payload["polygons"] == [
        {
            "points": [[0.0, 0.0], [1000.0, 0.0], [1000.0, 2000.0], [0.0, 2000.0]],
            "load": 8.1,
        },
        {
            "points": [[0.0, 0.0], [1000.0, 2000.0], [0.0, 2000.0]],
            "load": 12.2,
        },
    ]


@pytest.mark.parametrize(
    ("load_column", "expected"),
    [(1, 7.2), (2, 8.1), (3, 9.2), (4, 10.3)],
)
def test_xlsx_parser_selects_as1_through_as4(load_column: int, expected: float):
    nodes, elements, loads = _sample_tables(second_load_row=777.0)

    payload = _parser()(nodes, elements, loads, load_column)

    assert payload["polygons"][0]["load"] == expected


def test_xlsx_parser_uses_first_load_row_for_each_element():
    nodes, elements, loads = _sample_tables(second_load_row=12345.0)

    payload = _parser()(nodes, elements, loads, 4)

    assert payload["polygons"][0]["load"] == 10.3
    assert payload["polygons"][1]["load"] == 14.4


@pytest.mark.parametrize("load_column", [0, 5, -1])
def test_xlsx_parser_rejects_invalid_load_column(load_column: int):
    nodes, elements, loads = _sample_tables()

    with pytest.raises(ValueError, match="1..4"):
        _parser()(nodes, elements, loads, load_column)


def test_xlsx_parser_rejects_duplicate_node_ids():
    nodes, elements, loads = _sample_tables()
    duplicate_nodes = _xlsx(
        [
            ["№ узла", "X", "Y", "Z"],
            [1, 0, 0, 0],
            [1, 1, 0, 0],
        ]
    )

    with pytest.raises(ValueError, match="Duplicate node id 1"):
        _parser()(duplicate_nodes, elements, loads, 1)


def test_xlsx_parser_rejects_missing_referenced_node():
    nodes = _xlsx(
        [
            ["№ узла", "X", "Y", "Z"],
            [1, 0, 0, 0],
            [2, 1, 0, 0],
            [3, 1, 1, 0],
        ]
    )
    elements = _xlsx([["№ элем", "№№ узлов"], [10, "1,2,99"]])
    loads = _xlsx([["Элемент", "AS1", "AS2", "AS3", "AS4"], [10, 1, 2, 3, 4]])

    with pytest.raises(ValueError, match="unknown node 99"):
        _parser()(nodes, elements, loads, 1)


def test_xlsx_parser_rejects_missing_load_for_element():
    nodes, elements, _ = _sample_tables()
    loads = _xlsx([["Элемент", "AS1", "AS2", "AS3", "AS4"], [10, 1, 2, 3, 4]])

    with pytest.raises(ValueError, match="Missing load for element 11"):
        _parser()(nodes, elements, loads, 1)


def test_xlsx_parser_rejects_non_numeric_selected_load():
    nodes, elements, _ = _sample_tables()
    loads = _xlsx(
        [
            ["Элемент", "AS1", "AS2", "AS3", "AS4"],
            [10, 1, "---", 3, 4],
            [11, 1, 2, 3, 4],
        ]
    )

    with pytest.raises(ValueError, match="AS2.*element 10"):
        _parser()(nodes, elements, loads, 2)


def test_xlsx_parser_rejects_missing_required_headers():
    nodes, elements, loads = _sample_tables()
    bad_elements = _xlsx([["wrong", "headers"], [10, "1,2,3"]])

    with pytest.raises(ValueError, match="elements.*header"):
        _parser()(nodes, bad_elements, loads, 1)


def test_source_polygons_from_input_returns_polygon_tasks_in_mm():
    result = source_polygons.source_polygons_from_input(
        {
            "kind": "polygons",
            "units": "m",
            "polygons": [{"points": [[0, 0], [1, 0], [1, 2]], "load": 4.5}],
        }
    )

    assert result == [
        {"points": [[0.0, 0.0], [1000.0, 0.0], [1000.0, 2000.0]], "load": 4.5}
    ]
