from __future__ import annotations

import numpy as np

from A101.read_dxf import _is_usable_face


def test_zero_area_3dface_is_not_usable() -> None:
    points = np.array(
        [
            [10040.0, 14810.0],
            [10040.0, 14810.0],
            [10420.0, 14810.0],
            [10420.0, 14810.0],
        ]
    )

    assert _is_usable_face(points) is False


def test_triangle_with_repeated_fourth_vertex_is_usable() -> None:
    points = np.array(
        [
            [0.0, 0.0],
            [1000.0, 0.0],
            [0.0, 1000.0],
            [0.0, 1000.0],
        ]
    )

    assert _is_usable_face(points) is True
