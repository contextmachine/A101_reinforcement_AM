"""Human-readable diagnostics for a task that cannot be solved as configured.

The pipelines store the raw facts in ``prepare_info`` (``reason`` + ``details``) and a Russian
``message`` that goes into the task's and every N's ``error`` field, so the front end can show why
nothing was computed and what would fix it without decoding internal fields.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence


def _catalog_max(back_grid: Sequence[float] | None, max_layers: int | None) -> str:
    bg = "" if not back_grid else f" + фон ф{int(back_grid[0])}@{int(back_grid[1])}"
    layers = "" if not max_layers else f" при max_layers={int(max_layers)}"
    return f"{layers}{bg}"


def capacity_details(exc: Any, rows: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Facts of a ``ReinforcementCapacityError``: what is needed, what the stock can give, who is affected."""
    load = float(exc.load)
    limit = float(exc.max_supported_load)
    details: dict[str, Any] = {
        "load": load, "max_supported_load": limit, "shortfall": round(load - limit, 2),
        "max_layers": exc.max_layers, "back_grid": None if exc.back_grid is None else list(exc.back_grid),
    }
    if rows:
        from shapely.geometry import Polygon

        over = [r for r in rows if float(r.get("load", 0) or 0) > limit + 1e-9]
        by_load = Counter(round(float(r.get("load", 0) or 0), 1) for r in over)
        area = 0.0
        for r in over:
            try:
                area += Polygon([(float(x), float(y)) for x, y in r["points"]]).area
            except Exception:  # noqa: BLE001 - area is informational
                pass
        details.update({
            "elements": len(over), "elements_total": len(rows), "area_m2": round(area / 1e6, 2),
            "elements_by_load": {str(k): v for k, v in sorted(by_load.items())},
        })
    return details


def capacity_message(details: Mapping[str, Any]) -> str:
    where = ""
    if details.get("elements") is not None:
        where = f" ({details['elements']} элем. из {details.get('elements_total')}, {details.get('area_m2')} м²)"
    return (
        f"Недостаточно армирования: требуется {details['load']:.1f} см²/м{where}, "
        f"максимум набора{_catalog_max(details.get('back_grid'), details.get('max_layers'))} — "
        f"{details['max_supported_load']:.1f} см²/м (не хватает {details['shortfall']:.1f}). "
        f"Добавьте более тяжёлый стержень в stock или увеличьте max_layers."
    )


def candidate_cover_message(detail: str) -> str:
    return f"Не удалось построить покрытие поля прямоугольниками: {detail}"
