import numpy as np

def orient_grid(matrix, xs, ys, direction):
    direction = direction.lower()

    if direction == "y":
        return (
            matrix,
            xs,
            ys,
            np.diff(xs),
            np.diff(ys),
        )

    if direction == "x":
        return (
            matrix.T,
            ys,
            xs,
            np.diff(ys),
            np.diff(xs),
        )

    raise ValueError("direction должен быть 'x' или 'y'")


def restore_rectangles(rectangles, direction):
    """
    rectangles: (x1, y1, x2, y2, w)
    """
    if direction.lower() == "y":
        return rectangles

    return [
        (y1, x1, y2, x2, w)
        for x1, y1, x2, y2, w in rectangles
    ]