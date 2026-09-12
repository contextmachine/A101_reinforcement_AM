"""Wire models of the /v2 API.

Every model is a faithful pydantic v2 translation of ``payload-v2-am-aa.md``. The
request bodies forbid unknown fields, geometry rejects NaN/Inf, and the few contract
names that are not valid Python identifiers (``need_load_sm2/m`` and friends) are
expressed through ``Field(alias=...)`` with ``populate_by_name`` so both spellings
validate while only the contract spelling is serialised.
"""
from __future__ import annotations

from math import hypot
from typing import Any, Annotated, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Point = tuple[float, float]

#: Per-N lifecycle of an optimisation task.
N_STATES = ("pending", "preparing", "solving", "fitting", "bars", "success", "error", "cancelled")
#: Per-N solver outcome; the contract spelling (``feasable``) is intentional.
N_STATUSES = ("optimal", "feasable", "infeasable")
#: Lifecycle of a /v2/bars and /v2/verification task.
JOB_STATES = ("pending", "running", "success", "error")
#: Overlay state vocabulary of the wire format (see ``scenes.to_wire_overlay_state``).
WIRE_OVERLAY_STATES = ("active", "real", "empty")


class V2Model(BaseModel):
    """Strict base: unknown fields are rejected and aliases may be bypassed by name."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, allow_inf_nan=False)


def _positive_unique_ns(value: Sequence[int]) -> list[int]:
    values = list(dict.fromkeys(int(v) for v in value))
    if not values:
        raise ValueError("n не должен быть пустым")
    if any(v < 1 for v in values):
        raise ValueError("n должен содержать положительные целые значения")
    return values


# ---------------------------------------------------------------- primitives


class RcVariant(V2Model):
    """Reinforcement variant: bar diameter and spacing, both in millimetres."""

    d: float = Field(gt=0)
    step: float = Field(gt=0)


class Anchorage(V2Model):
    """Anchorage length added invisibly to both ends of a bar or zone, millimetres."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)


class MassGroup(V2Model):
    with_anchorage_kg: float
    without_anchorage_kg: float
    with_anchorage_unclipped_kg: float
    without_anchorage_unclipped_kg: float


class MassMetrics(V2Model):
    additional: MassGroup
    bg: MassGroup


# --------------------------------------------------------------------- zones


class ZoneBase(V2Model):
    """Background reinforcement of the whole field."""

    id: int
    kind: Literal["bg"] = "bg"
    arm: RcVariant
    anchorage: Anchorage | None = None


class ZoneAdditional(V2Model):
    """One arithmetic run of additional bars around a base bar."""

    id: int
    kind: Literal["additional"] = "additional"
    arm: RcVariant
    left: int = Field(default=0, ge=0)
    right: int = Field(default=0, ge=0)
    length: float = Field(gt=0)
    anchorage: Anchorage | None = None
    origin: Point
    direction: Point

    @model_validator(mode="before")
    @classmethod
    def accept_contract_spelling(cls, value: Any) -> Any:
        # payload-v2-am-aa.md spells the key ``ancorage`` in the ZoneAdditional example.
        if isinstance(value, dict) and "ancorage" in value and "anchorage" not in value:
            value = dict(value)
            value["anchorage"] = value.pop("ancorage")
        return value

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, value: Point) -> Point:
        if abs(hypot(float(value[0]), float(value[1])) - 1.0) > 1e-5:
            raise ValueError("direction должен быть единичным вектором")
        return value


Zone = Annotated[ZoneBase | ZoneAdditional, Field(discriminator="kind")]


def validate_zone_collection(zones: Sequence[ZoneBase | ZoneAdditional]) -> None:
    """Reject duplicate zone ids and anything but exactly one background zone."""

    ids = [int(zone.id) for zone in zones]
    if len(set(ids)) != len(ids):
        raise ValueError("id зон должны быть уникальны в пределах запроса")
    backgrounds = sum(1 for zone in zones if zone.kind == "bg")
    if backgrounds != 1:
        raise ValueError("требуется ровно одна зона kind=bg")


class Bar(V2Model):
    """One laid-out bar segment; ``start``/``end`` never include the anchorage."""

    zone_id: int
    start: Point
    end: Point
    d: float = Field(gt=0)
    anchorage: Anchorage


# ------------------------------------------------------------------ polygons


class FEPolygon(V2Model):
    """Geometry and demand of one finite element of the uploaded mosaic."""

    load: float
    color: int | None = None
    points: list[Point] = Field(min_length=3)


class FEPolygonOut(FEPolygon):
    """Scene polygon annotated with its state at one overlay revision."""

    overlay_state: Literal["active", "real", "empty"]
    source_index: int


# ------------------------------------------------------------------ overlays


class OverlayIn(V2Model):
    """One eraser stroke: ``clean`` masks the listed polygons, ``unclean`` restores them."""

    type: Literal["clean", "unclean"]
    idxs: list[int] = Field(default_factory=list)
    real: bool = False
    time: int | float | None = None


class OverlayOut(OverlayIn):
    """Stored overlay event with its server-assigned id."""

    id: int


class OverlaysPost(V2Model):
    scene_id: str | None = None
    overlays: list[OverlayIn] = Field(default_factory=list)


class OverlayCreated(V2Model):
    scene_id: str
    overlay_id: int


class V2SceneCreated(V2Model):
    scene_id: str
    state: str


# --------------------------------------------------------------------- tasks


class SolverConfig(V2Model):
    solver_time_limit: float | None = Field(default=None, gt=0)


class TaskConfig(V2Model):
    max_layers: int | None = Field(default=None, ge=1)
    axis: Literal["x", "y"] = "y"
    anchor_factor: float = Field(default=40.0, ge=0)
    min_width_mm: float = Field(default=1000.0, gt=0)
    max_snap_mm: float = Field(default=600.0, ge=0)
    min_bar_gap_mm: float | None = Field(default=None, gt=0)
    steel_density_kg_m3: float = Field(default=7850.0, gt=0)
    back_grid: RcVariant | None = None
    stock: list[RcVariant] | None = Field(default=None, min_length=1)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    cover_mm: float = Field(default=30.0, ge=0)
    fill_gaps: bool = True


class V2TaskCreate(V2Model):
    scene_id: str = Field(min_length=1)
    overlay_id: int = 0
    smooth: bool = False
    n: list[int] = Field(min_length=1)
    config: TaskConfig = Field(default_factory=TaskConfig)

    @field_validator("n")
    @classmethod
    def validate_n(cls, value: list[int]) -> list[int]:
        return _positive_unique_ns(value)


class V2TaskCreated(V2Model):
    task_id: str


class V2NMutation(V2Model):
    task_id: str | None = None
    n: list[int] = Field(min_length=1)

    @field_validator("n")
    @classmethod
    def validate_n(cls, value: list[int]) -> list[int]:
        return _positive_unique_ns(value)


class V2CancelMutation(V2Model):
    task_id: str | None = None
    n: list[int] | None = None

    @field_validator("n")
    @classmethod
    def validate_n(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        return _positive_unique_ns(value)


class V2SolutionSummary(V2Model):
    n: int
    state: str
    fun: float | None = None
    status: str | None = None
    mass_metrics: MassMetrics | None = None


class TaskView(V2Model):
    task_id: str
    scene_id: str
    smooth: bool = False
    overlay_id: int = 0
    solutions: list[V2SolutionSummary] = Field(default_factory=list)


class SolutionView(V2SolutionSummary):
    task_id: str
    bars: list[Bar] | None = None
    zones: list[Zone] | None = None
    repair: dict[str, Any] | None = None
    solver: dict[str, Any] | None = None
    error: str | None = None


# ------------------------------------------------------- bars / verification


class BarsConfig(V2Model):
    axis: Literal["x", "y"] = "y"
    anchor_factor: float = Field(default=40.0, ge=0)
    min_bar_gap_mm: float | None = Field(default=None, gt=0)
    # Concrete cover to the bar face (защитный слой), mm: sets the lateral reach 5·(cover + d/2)
    # of every rod in the smeared-density verification (EN 1992-1-1 §7.3.4).
    cover_mm: float = Field(default=30.0, ge=0)
    # After the layout, check every polygon with the verification model and insert one rod into the
    # widest gap of parallel rods over each polygon that is still short; repeat until covering.
    fill_gaps: bool = True


class BarsRequest(V2Model):
    scene_id: str = Field(min_length=1)
    smooth: bool = False
    overlay_id: int = 0
    config: BarsConfig = Field(default_factory=BarsConfig)
    zones: list[Zone] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_zones(self):
        validate_zone_collection(self.zones)
        return self


class BarsCreated(V2Model):
    task_id: str
    state: str


class BarsView(V2Model):
    task_id: str
    state: str
    bars: list[Bar] | None = None
    zones: list[Zone] | None = None
    mass_metrics: MassMetrics | None = None
    repair: dict[str, Any] | None = None
    error: str | None = None


class VerificationConfig(BarsConfig):
    steel_density_kg_m3: float = Field(default=7850.0, gt=0)
    t: float = Field(gt=0)


class VerificationRequest(V2Model):
    scene_id: str = Field(min_length=1)
    smooth: bool = False
    overlay_id: int = 0
    config: VerificationConfig
    zones: list[Zone] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_zones(self):
        validate_zone_collection(self.zones)
        return self


class VerificationCreated(V2Model):
    task_id: str
    state: str


class VerificationRow(V2Model):
    """Per-polygon demand and actual reinforcement; field names carry the contract slash."""

    source_index: int
    overlay_state: str
    need_load_sm2_m: float | None = Field(default=None, alias="need_load_sm2/m")
    fact_load_sm2_m: float | None = Field(default=None, alias="fact_load_sm2/m")
    need_load_kg_m3: float | None = Field(default=None, alias="need_load_kg/m3")
    fact_load_kg_m3: float | None = Field(default=None, alias="fact_load_kg/m3")


class VerificationView(V2Model):
    verification_id: str
    state: str
    result: list[VerificationRow] | None = None
    error: str | None = None
