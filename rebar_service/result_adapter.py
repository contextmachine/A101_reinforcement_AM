from __future__ import annotations

from typing import Any, Dict, Mapping


def to_legacy_result(solution: Mapping[str, Any]) -> Dict[str, Any]:
    """Preserve the result shape used by legacy frontend/exporters."""
    row = dict(solution)
    choices = row.get("component_choices", {}) or {}
    solver_results = {
        str(cid): dict(value.get("solver_result", {}) or {})
        for cid, value in choices.items()
        if isinstance(value, Mapping)
    }
    fit_results = {
        str(cid): dict(value.get("fit_result", {}) or {})
        for cid, value in choices.items()
        if isinstance(value, Mapping)
    }
    rectangles = list(row.get("rectangles", []) or [])
    anchored = list(row.get("anchored_boxes", []) or [])
    actual_mass = row.get("actual_mass_kg")
    proxy_mass = row.get("proxy_mass")
    return {
        "is_feasible": bool(row.get("is_feasible")),
        "is_optimal": bool(row.get("is_optimal", False)),
        "status": row.get("status", "feasible" if row.get("is_feasible") else "infeasible"),
        "total_cost": proxy_mass,
        "solver_result": {
            "is_feasible": bool(row.get("is_feasible")),
            "component_results": solver_results,
            "component_ns": dict(row.get("component_ns", {}) or {}),
        },
        "primary_rectangles": rectangles or anchored,
        "rectangles": rectangles,
        "anchored_boxes": anchored,
        "fit_result": {
            "is_feasible": bool(row.get("is_feasible")),
            "component_results": fit_results,
            "rectangles": rectangles,
        },
        "summary": {
            "mass": actual_mass,
            "mass_kg": actual_mass,
            "mass with anchorage": actual_mass,
            "proxy_mass": proxy_mass,
            "N": int(row.get("total_N", 0)),
        },
        "solution_id": row.get("solution_id"),
        "source": row.get("source", "components"),
        "total_N": int(row.get("total_N", 0)),
        "component_ns": dict(row.get("component_ns", {}) or {}),
        "proxy_mass": proxy_mass,
        "actual_mass_kg": actual_mass,
        "bar_layout": dict(row.get("bar_layout", {}) or {}),
        "metadata": dict(row.get("metadata", {}) or {}),
    }
