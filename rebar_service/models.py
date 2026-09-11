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
    timeout_seconds: float | None = Field(default=None, gt=0)
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
    back_grid: tuple[int, int] | None = None
    stock: list[tuple[int, int]] | None = Field(default=None, min_length=1)
    max_layers: int | None = Field(default=None, ge=1)
    min_width_mm: float = Field(default=1000.0, gt=0)
    steel_density_kg_m3: float = Field(default=7850.0, gt=0)
    anchor_factor: float = Field(default=40.0, ge=0)
    axis: Literal["x", "y"] = "y"
    max_snap_mm: float = Field(default=600.0, ge=0)
    min_bar_gap_mm: float = Field(default=50.0, ge=0)
    scan_mode: Literal["requested", "hard"] = "requested"
    whole: bool = False
    component_result_top_k: int = Field(default=5, ge=1, le=100)
    validate_results: bool = False
    max_concurrent_jobs: int | None = Field(default=None, ge=1)
    quantizer: QuantizerOptions = Field(default_factory=QuantizerOptions)
    solver: SolverOptions = Field(default_factory=SolverOptions)

    @field_validator("back_grid")
    @classmethod
    def validate_grid(cls, value):
        if value is None:
            return value
        if value[0] <= 0 or value[1] <= 0:
            raise ValueError("Диаметр и шаг должны быть положительными")
        return value

    @field_validator("stock")
    @classmethod
    def validate_stock(cls, value):
        if value is None:
            return value
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
    overlay: int = 0
    action: Literal["add", "cancel", "pause_range", "resume_range", "cancel_task", "snapshot"]
    n: int | list[int] | None = None
    smooth: bool = False


class SceneCreated(BaseModel):
    scene_id: str
    state: str


class SceneInfo(SceneCreated):
    components: list[dict] = Field(default_factory=list)


class TaskCreated(BaseModel):
    task_id: str
    state: str
    websocket_url: str
    status_url: str
    scene_id: str | None = None
    smooth: bool | None = None
    overlay_id: int | None = None


class AnalysisTaskStart(TaskParameters):
    config: dict | str | None = Field(
        default=None,
        description="Конфигурация как в upload (объект или JSON-строка). n задаётся снаружи."
    )

    @model_validator(mode="before")
    @classmethod
    def expand_upload_config(cls, value):
        import json
        if not isinstance(value, dict):
            return value
        data = dict(value)
        config = data.get("config")
        if config is None:
            return data
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (ValueError, TypeError) as exc:
                raise ValueError("config должен быть корректным JSON-объектом") from exc
        if not isinstance(config, dict):
            raise ValueError("config должен быть объектом или JSON-строкой объекта")
        reserved = {"scene_id", "overlay_id", "smooth", "components", "config"}
        if reserved.intersection(config):
            raise ValueError("scene_id, overlay_id, smooth и components задаются вне config")
        for key, item in config.items():
            if key in data and data[key] != item:
                raise ValueError(f"Параметр {key} задан в config и снаружи с разными значениями")
            data.setdefault(key, item)
        data["config"] = dict(config)
        return data

    scene_id: str = Field(min_length=1)
    overlay_id: int = 0
    smooth: bool = False
    n: list[int] = Field(min_length=1)
    components: list[int] = Field(default_factory=lambda: [-2], min_length=1)

    @field_validator("n")
    @classmethod
    def validate_analysis_n(cls, value: list[int]) -> list[int]:
        values = list(dict.fromkeys(int(v) for v in value))
        if not values or any(v < 1 for v in values):
            raise ValueError("n должен содержать положительные целые значения")
        if any(v > 250 for v in values):
            raise ValueError("N одного solver-запуска не может превышать 250")
        return values

    @field_validator("components")
    @classmethod
    def validate_components(cls, value: list[int]) -> list[int]:
        values = list(dict.fromkeys(int(v) for v in value))
        if not values:
            raise ValueError("components не должен быть пустым")
        specials = {v for v in values if v < 0}
        if specials:
            if len(values) != 1 or next(iter(specials)) not in {-1, -2, -3}:
                raise ValueError("-1/-2/-3 допустимы только как единственное значение components")
        elif any(v < 0 for v in values):
            raise ValueError("индексы components должны быть неотрицательными")
        return values


class CompactZone(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    origin: tuple[float, float]
    direction: tuple[float, float]
    length: float = Field(gt=0)
    step: float = Field(gt=0)
    right: int = Field(default=0, ge=0)
    left: int = Field(default=0, ge=0)
    d: float = Field(gt=0)

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, value):
        from math import hypot
        norm = hypot(float(value[0]), float(value[1]))
        if abs(norm - 1.0) > 1e-5:
            raise ValueError("direction должен быть единичным вектором")
        return value


class CompactVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    scene_id: str = Field(min_length=1)
    smooth: bool = False
    overlay_id: int = 0
    zones: list[CompactZone] = Field(default_factory=list)
    back_grid: tuple[float, float] | None = None

    @field_validator("back_grid")
    @classmethod
    def validate_optional_grid(cls, value):
        if value is not None and (value[0] <= 0 or value[1] <= 0):
            raise ValueError("back_grid diameter/step должны быть положительными")
        return value


class StoredSolutionVerificationRequest(BaseModel):
    scene_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    n: int = Field(ge=1)
    component_id: int | None = None


class VerificationResponse(BaseModel):
    scene_id: str
    task_id: str | None = None
    n: int | None = None
    component_id: int | None = None
    smooth: bool
    overlay_id: int
    coverage: list[float | None]


class ResultEnvelope(BaseModel):
    task_id: str
    n: int
    status: str
    result: dict | None = None


class ComponentNRequest(BaseModel):
    n: list[int] = Field(min_length=1)

    @field_validator("n")
    @classmethod
    def validate_n(cls, value: list[int]) -> list[int]:
        values = list(dict.fromkeys(int(v) for v in value))
        if not values or any(v < 1 for v in values):
            raise ValueError("n должен содержать положительные целые значения")
        return values


class OverlayEventMutation(BaseModel):
    type: Literal["clean", "unclean"]
    idxs: list[int] = Field(default_factory=list)
    id: int = Field(gt=0)
    real: bool = False

    @field_validator("idxs")
    @classmethod
    def validate_idxs(cls, value: list[int]) -> list[int]:
        values = list(dict.fromkeys(int(v) for v in value))
        if any(v < 0 for v in values):
            raise ValueError("idxs должен содержать неотрицательные индексы source polygons")
        return values


class ComponentInfo(BaseModel):
    id: int
    polygon_indices: list[int] = Field(default_factory=list)
    classes: list[int] = Field(default_factory=list)
    loads: list[float] = Field(default_factory=list)
    bounds: list[float] | None = None
    demand_bounds: list[float] | None = None
    max_useful_n: int | None = None
    prepared: bool = False
    state: str = "created"


class SolutionSummary(BaseModel):
    solution_id: str
    source: str
    total_N: int
    component_ns: dict[str, int] = Field(default_factory=dict)
    proxy_mass: float | None = None
    actual_mass_kg: float | None = None
    is_feasible: bool = False
    status: str = "unknown"
    result_url: str | None = None
