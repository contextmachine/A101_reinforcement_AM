from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, validator


class ScanMode(str, Enum):
    requested = "requested"
    hard = "hard"


class ComponentOptions(BaseModel):
    scan_mode: ScanMode = ScanMode.requested
    whole: bool = False
    component_result_top_k: int = Field(5, ge=1, le=100)
    validate_results: bool = False

    class Config:
        extra = "allow"


class ComponentNRequest(BaseModel):
    n: List[int]

    @validator("n")
    def validate_n(cls, value: List[int]) -> List[int]:
        values = list(dict.fromkeys(int(v) for v in value))
        if not values or any(v < 1 for v in values):
            raise ValueError("n должен содержать положительные целые значения")
        return values


class ComponentInfo(BaseModel):
    id: int
    polygon_indices: List[int] = []
    classes: List[int] = []
    loads: List[float] = []
    bounds: Optional[List[float]] = None
    demand_bounds: Optional[List[float]] = None
    max_useful_n: Optional[int] = None
    prepared: bool = False
    state: str = "created"


class SolutionSummary(BaseModel):
    solution_id: str
    source: str
    total_N: int
    component_ns: Dict[str, int] = {}
    proxy_mass: Optional[float] = None
    actual_mass_kg: Optional[float] = None
    is_feasible: bool = False
    status: str = "unknown"
    result_url: Optional[str] = None
