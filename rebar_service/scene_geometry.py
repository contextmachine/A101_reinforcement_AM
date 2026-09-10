from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from shapely.strtree import STRtree

from .polygon_storage import geometry_polygons


class _DSU:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left == right:
            return
        if self.rank[left] < self.rank[right]:
            left, right = right, left
        self.parent[right] = left
        if self.rank[left] == self.rank[right]:
            self.rank[left] += 1


def build_stable_scene_components(
    polygons: Sequence[Mapping[str, Any]],
    *,
    tolerance: float = 1e-6,
) -> list[dict[str, Any]]:
    """Build stable, config-independent physical components for a scene.

    Components are based on the uploaded raw physical polygons only.  They are
    intentionally independent of reinforcement recipes, smoothing and overlays,
    so component ids remain stable for the whole lifetime of the scene.
    """

    rows = geometry_polygons(polygons)
    geometries = [row["geometry"] for row in rows]
    if not geometries:
        return []

    tree = STRtree(geometries)
    dsu = _DSU(len(geometries))
    eps = max(0.0, float(tolerance))

    for i, geometry in enumerate(geometries):
        query_geometry = geometry if eps <= 0 else geometry.buffer(eps)
        for j in map(int, tree.query(query_geometry, predicate="intersects")):
            if j > i:
                dsu.union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(geometries)):
        groups[dsu.find(index)].append(index)

    ordered = sorted(groups.values(), key=lambda values: min(values))
    result: list[dict[str, Any]] = []
    for component_id, indices in enumerate(ordered):
        xmin = min(float(geometries[i].bounds[0]) for i in indices)
        ymin = min(float(geometries[i].bounds[1]) for i in indices)
        xmax = max(float(geometries[i].bounds[2]) for i in indices)
        ymax = max(float(geometries[i].bounds[3]) for i in indices)
        result.append(
            {
                "id": int(component_id),
                "polygon_indices": [int(i) for i in sorted(indices)],
                "bounds": [xmin, ymin, xmax, ymax],
            }
        )
    return result
