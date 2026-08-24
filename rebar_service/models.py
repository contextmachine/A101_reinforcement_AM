from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PolygonLoad(BaseModel):
    points: list[tuple[float, float]] = Field(min_length=3)
    load: float


class PolygonInput(BaseModel):
    kind: Literal["polygons"] = "polygons"
    units: Literal["mm", "m"] = "mm"
    polygons: list[PolygonLoad] = Field(min_length=1)


class RangeN(BaseModel):
    start: int = Field(ge=0)
    stop: int = Field(ge=0)
    coarse_step: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self):
        if self.stop < self.start:
            raise ValueError("stop должен быть не меньше start")
        return self


NRequest = int | list[int] | RangeN


class QuantizerOptions(BaseModel):
    method: Literal["exact", "heuristic"] = "exact"
    preserve_holes: bool = True
    max_shift_fraction: float = 0.02
    shrink_penalty: float = 30.0
    expand_penalty: float = 1.0
    load_gamma: float = 2.5
    min_shrink_tol_ratio: float = 0.10
    min_expand_tol_ratio: float = 0.50
    coord_eps: float = 1e-6
    target_cells_x: int | None = Field(default=None, ge=1)
    target_cells_y: int | None = Field(default=None, ge=1)


class SolverOptions(BaseModel):
    backend: Literal["highs", "cbc"] = "highs"
    timeout_seconds: float = Field(default=120.0, gt=0)
    solver_time_limit: float | None = Field(default=None, gt=0)
    threads: int = Field(default=1, ge=1)
    require_optimal: bool = True
    return_best_on_timeout: bool = True
    use_warm_start: bool = True
    cross_n_warm_start: bool = True
    emit_interval: float = Field(default=5.0, gt=0)
    emit_every_nodes: int | None = Field(default=None, ge=1)
    emit_heartbeat: bool = True
    highs_options: dict[str, object] = Field(default_factory=dict)
    prepared_max_n: int | None = Field(default=None, ge=0)
    build_pulp_template: bool = False
    postprocess_intermediate: bool = False


class TaskParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: NRequest
    back_grid: tuple[int, int] = (18, 300)
    stock: list[tuple[int, int]] = Field(
        default_factory=lambda: [(18, 300), (20, 150), (20, 100), (25, 150), (25, 100)],
        min_length=1,
    )
    max_layers: int = Field(default=2, ge=1)
    # Пиковые нагрузки выше max_layers слоёв (обычно единичные КЭ у колонн)
    # срезаются до максимума и возвращаются в preparation.peak_loads.
    clamp_peak_loads: bool = True
    min_width_mm: float = Field(default=1000.0, gt=0)
    steel_density_kg_m3: float = Field(default=7850.0, gt=0)
    anchor_factor: float = Field(default=32.0, ge=0)
    axis: Literal["x", "y"] = "y"
    max_snap_mm: float = Field(default=600.0, ge=0)
    min_bar_gap_mm: float = Field(default=50.0, ge=0)
    max_concurrent_jobs: int | None = Field(default=None, ge=1)
    quantizer: QuantizerOptions = Field(default_factory=QuantizerOptions)
    solver: SolverOptions = Field(default_factory=SolverOptions)

    @field_validator("back_grid")
    @classmethod
    def validate_grid(cls, value):
        if value[0] <= 0 or value[1] <= 0:
            raise ValueError("Диаметр и шаг должны быть положительными")
        return value

    @field_validator("stock")
    @classmethod
    def validate_stock(cls, value):
        if any(d <= 0 or step <= 0 for d, step in value):
            raise ValueError("Все stock-пары должны быть положительными")
        return value


class TaskCreate(TaskParameters):
    input: PolygonInput


class NMutation(BaseModel):
    n: int | list[int]


class CancelMutation(BaseModel):
    n: list[int] | None = None


class WsCommand(BaseModel):
    action: Literal["add", "cancel", "pause_range", "resume_range", "cancel_task", "snapshot"]
    n: int | list[int] | None = None


class TaskCreated(BaseModel):
    task_id: str
    state: str
    websocket_url: str
    status_url: str


class ResultEnvelope(BaseModel):
    task_id: str
    n: int
    status: str
    result: dict | None = None
