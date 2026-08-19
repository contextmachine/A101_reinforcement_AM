/**
 * Domain model.
 *
 * The wire format mirrors the solver's JSON output verbatim (space-separated
 * keys included) so that a result file produced by the backend can be dropped
 * into the UI untouched. Everything the UI actually works with is normalised
 * into the camelCase shapes below by `normalizeResult`.
 */

/** [x1, y1, x2, y2] in millimetres, axis-aligned, x1<=x2 / y1<=y2. */
export type RectXYXY = [number, number, number, number];

/** [x1, y1, x2, y2] in millimetres — a single rebar drawn as a segment. */
export type BarSegment = [number, number, number, number];

/** Raw zone as emitted by the solver. */
export interface RawZone {
  'primary rectangle': RectXYXY | null;
  'final rectangle': RectXYXY;
  width: number;
  length: number;
  diameter: number;
  step: number;
  'bars count': number;
  'zone mass': number;
  bars: BarSegment[];
}

/** Raw solver output. */
export interface RawResult {
  /** Number of zones. */
  N: number;
  /** Total additional-reinforcement mass, kg. */
  mass: number;
  zones: RawZone[];
  /** Optional echo of the parameters the job ran with. */
  params?: Partial<SolverParams>;
  /** Optional bar direction; defaults to 'y' (bars run along Y). */
  direction?: Axis;
}

export type Axis = 'x' | 'y';

/** A (diameter, step) pair, both in millimetres. */
export interface RebarSpec {
  diameter: number;
  step: number;
}

export interface SolverParams {
  /** Direction the additional bars run along. */
  direction: Axis;
  /** Background mesh already present in the slab. */
  back_grid: RebarSpec;
  /** Rebar options the solver may choose from. */
  stock: RebarSpec[];
  /** Maximum number of additional layers on top of the background mesh. */
  max_lay: number;
  /** Minimum zone width, mm. */
  min_w: number;
  /** Steel density, kg/m³. */
  iron_dens: number;
  /** Anchorage length factor: anchorage = anchor_k · diameter. */
  anchor_k: number;
}

/** Normalised zone, enriched with everything the UI needs. */
export interface Zone {
  /** 0-based index in the result. */
  index: number;
  primaryRect: RectXYXY | null;
  rect: RectXYXY;
  /** Extent across the bars, mm. */
  width: number;
  /** Bar length, mm. */
  length: number;
  diameter: number;
  step: number;
  barsCount: number;
  /** Mass as reported by the solver, kg. */
  mass: number;
  bars: BarSegment[];
  /** Σ of bar lengths, mm — measured from the segments, not trusted from `length`. */
  totalBarLength: number;
  /** Σ of bar lengths including 2 · anchor_k · d per bar, mm. */
  totalBarLengthAnchored: number;
  /** Mass including anchorage, kg. */
  massAnchored: number;
  /** Area of the final rectangle, mm². */
  area: number;
  /** kg of additional rebar per m² of the zone. */
  massPerArea: number;
  /** Stable key for the (diameter, step) pair, e.g. "d18@300". */
  specKey: string;
}

/** Aggregate for one (diameter, step) pair across the whole slab. */
export interface SpecTotals {
  specKey: string;
  diameter: number;
  step: number;
  zones: number;
  bars: number;
  /** metres */
  length: number;
  lengthAnchored: number;
  /** kg */
  mass: number;
  massAnchored: number;
  /** Share of total mass, 0..1 */
  massShare: number;
  /** Whether the pair is present in the configured stock. */
  inStock: boolean;
}

export type WarningLevel = 'info' | 'warn';

export interface Warning {
  level: WarningLevel;
  title: string;
  detail: string;
  /** Zone indices the warning refers to. */
  zones: number[];
}

export interface Summary {
  zoneCount: number;
  /** Solver-reported total mass, kg. */
  mass: number;
  /** Mass recomputed from bar geometry, kg — should match `mass`. */
  massComputed: number;
  /** |mass - massComputed| / mass, 0..1 */
  massDelta: number;
  massAnchored: number;
  bars: number;
  /** metres */
  length: number;
  lengthAnchored: number;
  /** m² covered by zone rectangles (overlaps counted once). */
  coveredArea: number;
  /** m² of the drawing bounding box. */
  boundingArea: number;
  /** kg per m² of covered area. */
  massPerCoveredArea: number;
  /** kg per m² of the bounding box. */
  massPerBoundingArea: number;
  bounds: RectXYXY;
  bySpec: SpecTotals[];
  /** Zones sorted by mass, descending. */
  heaviest: Zone[];
  /** Number of heaviest zones making up >= 80% of the mass. */
  paretoCount: number;
  warnings: Warning[];
}

export interface NormalizedResult {
  raw: RawResult;
  params: SolverParams;
  direction: Axis;
  zones: Zone[];
  summary: Summary;
}

/* ------------------------------------------------------------------ */
/* Job API                                                             */
/* ------------------------------------------------------------------ */

export type JobStatus = 'queued' | 'running' | 'done' | 'error';

export interface Job {
  jobId: string;
  status: JobStatus;
  /** 0..1 */
  progress: number;
  /** Human-readable current stage. */
  stage: string;
  fileName: string;
  createdAt: string;
  finishedAt?: string;
  error?: string;
  /** Available result artefacts, by format. */
  artifacts?: Partial<Record<ArtifactFormat, string>>;
}

export type ArtifactFormat = 'dwg' | 'dxf' | 'json';
