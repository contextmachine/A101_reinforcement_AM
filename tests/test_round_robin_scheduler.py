from rebar_service.planner import edge_to_middle_order, round_robin_unit_plans


def test_edge_to_middle_orders_requested_values_only():
    assert edge_to_middle_order(range(1, 8)) == [1, 7, 2, 6, 3, 5, 4]
    assert edge_to_middle_order([2, 4, 8, 12]) == [2, 12, 4, 8]


def test_round_robin_interleaves_unequal_unit_plans():
    plans = {
        0: edge_to_middle_order([1, 2, 3, 4, 5]),
        1: edge_to_middle_order([1, 2, 3, 4, 5, 6, 7]),
        "whole": edge_to_middle_order([1, 2, 3]),
    }
    assert round_robin_unit_plans(plans)[:6] == [
        (0, 1), (1, 1), ("whole", 1),
        (0, 5), (1, 7), ("whole", 3),
    ]
