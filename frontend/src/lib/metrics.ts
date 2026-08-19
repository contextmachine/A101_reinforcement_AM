import type {
  Axis,
  NormalizedResult,
  RawResult,
  RawZone,
  RectXYXY,
  RebarSpec,
  SolverParams,
  SpecTotals,
  Summary,
  Warning,
  Zone,
} from './types';

export const DEFAULT_PARAMS: SolverParams = {
  direction: 'y',
  back_grid: { diameter: 18, step: 300 },
  stock: [
    { diameter: 18, step: 300 },
    { diameter: 20, step: 150 },
    { diameter: 20, step: 100 },
    { diameter: 25, step: 150 },
    { diameter: 25, step: 100 },
  ],
  max_lay: 2,
  min_w: 1000,
  iron_dens: 7850,
  anchor_k: 32,
};

export const specKeyOf = (spec: RebarSpec) => `d${spec.diameter}@${spec.step}`;

/** Cross-sectional area of a bar, m², from a diameter in mm. */
export const barArea = (diameterMm: number) =>
  (Math.PI * (diameterMm / 1000) ** 2) / 4;

/** Mass of `lengthMm` of bar of `diameterMm`, in kg. */
export const barMass = (diameterMm: number, lengthMm: number, density: number) =>
  density * barArea(diameterMm) * (lengthMm / 1000);

const segLength = ([x1, y1, x2, y2]: [number, number, number, number]) =>
  Math.hypot(x2 - x1, y2 - y1);

const normRect = (r: RectXYXY): RectXYXY => [
  Math.min(r[0], r[2]),
  Math.min(r[1], r[3]),
  Math.max(r[0], r[2]),
  Math.max(r[1], r[3]),
];

/**
 * Area of the union of axis-aligned rectangles, in the same squared units as
 * the input. Coordinate compression — O(n²) cells, fine for the tens of zones
 * a slab produces.
 */
export function unionArea(rects: RectXYXY[]): number {
  if (rects.length === 0) return 0;
  const xs = [...new Set(rects.flatMap((r) => [r[0], r[2]]))].sort((a, b) => a - b);
  const ys = [...new Set(rects.flatMap((r) => [r[1], r[3]]))].sort((a, b) => a - b);
  let total = 0;
  for (let i = 0; i < xs.length - 1; i++) {
    const x0 = xs[i];
    const x1 = xs[i + 1];
    for (let j = 0; j < ys.length - 1; j++) {
      const y0 = ys[j];
      const y1 = ys[j + 1];
      const covered = rects.some((r) => r[0] <= x0 && r[2] >= x1 && r[1] <= y0 && r[3] >= y1);
      if (covered) total += (x1 - x0) * (y1 - y0);
    }
  }
  return total;
}

/** Bounding box of every zone rectangle and bar in the result. */
export function boundsOf(zones: Zone[]): RectXYXY {
  if (zones.length === 0) return [0, 0, 1, 1];
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const eat = (x: number, y: number) => {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  };
  for (const z of zones) {
    eat(z.rect[0], z.rect[1]);
    eat(z.rect[2], z.rect[3]);
    if (z.primaryRect) {
      eat(z.primaryRect[0], z.primaryRect[1]);
      eat(z.primaryRect[2], z.primaryRect[3]);
    }
    for (const b of z.bars) {
      eat(b[0], b[1]);
      eat(b[2], b[3]);
    }
  }
  return [minX, minY, maxX, maxY];
}

function normalizeZone(raw: RawZone, index: number, params: SolverParams): Zone {
  const rect = normRect(raw['final rectangle']);
  const bars = raw.bars ?? [];
  const totalBarLength = bars.reduce((acc, b) => acc + segLength(b), 0);
  // Anchorage is added at both ends of every bar.
  const anchorage = 2 * params.anchor_k * raw.diameter * bars.length;
  const totalBarLengthAnchored = totalBarLength + anchorage;
  const area = Math.max(0, (rect[2] - rect[0]) * (rect[3] - rect[1]));
  return {
    index,
    primaryRect: raw['primary rectangle'] ? normRect(raw['primary rectangle']) : null,
    rect,
    width: raw.width,
    length: raw.length,
    diameter: raw.diameter,
    step: raw.step,
    barsCount: raw['bars count'] ?? bars.length,
    mass: raw['zone mass'],
    bars,
    totalBarLength,
    totalBarLengthAnchored,
    massAnchored: barMass(raw.diameter, totalBarLengthAnchored, params.iron_dens),
    area,
    massPerArea: area > 0 ? raw['zone mass'] / (area / 1e6) : 0,
    specKey: specKeyOf({ diameter: raw.diameter, step: raw.step }),
  };
}

function buildWarnings(zones: Zone[], params: SolverParams, summary: {
  mass: number;
  massComputed: number;
}): Warning[] {
  const warnings: Warning[] = [];
  const stockKeys = new Set(params.stock.map(specKeyOf));

  const offStock = zones.filter((z) => !stockKeys.has(z.specKey));
  if (offStock.length) {
    const kinds = [...new Set(offStock.map((z) => `⌀${z.diameter}/${z.step}`))].join(', ');
    warnings.push({
      level: 'warn',
      title: 'Rebar outside the configured stock',
      detail: `${offStock.length} zone(s) use ${kinds}, which is not in the stock list you supplied. Either the job ran with a different stock, or the result predates the current settings.`,
      zones: offStock.map((z) => z.index),
    });
  }

  const narrow = zones.filter((z) => z.width < params.min_w - 1e-6);
  if (narrow.length) {
    warnings.push({
      level: 'warn',
      title: `Zones narrower than min_w (${params.min_w} mm)`,
      detail: `${narrow.length} zone(s) are ${Math.min(...narrow.map((z) => z.width))}–${Math.max(
        ...narrow.map((z) => z.width),
      )} mm wide. Narrow strips are usually residual patches — check they are constructible.`,
      zones: narrow.map((z) => z.index),
    });
  }

  const orphans = zones.filter((z) => z.primaryRect === null);
  if (orphans.length) {
    warnings.push({
      level: 'info',
      title: 'Zones without a primary rectangle',
      detail: `${orphans.length} zone(s) have no source rectangle from the optimisation stage — they were added afterwards to cover leftover demand.`,
      zones: orphans.map((z) => z.index),
    });
  }

  const countMismatch = zones.filter((z) => z.barsCount !== z.bars.length);
  if (countMismatch.length) {
    warnings.push({
      level: 'warn',
      title: 'Declared bar count differs from the geometry',
      detail: `${countMismatch.length} zone(s) report a "bars count" that does not match the number of bar segments.`,
      zones: countMismatch.map((z) => z.index),
    });
  }

  if (summary.mass > 0 && Math.abs(summary.mass - summary.massComputed) / summary.mass > 1e-6) {
    warnings.push({
      level: 'warn',
      title: 'Reported mass differs from the geometry',
      detail: `The file reports ${summary.mass.toFixed(1)} kg, but the bar segments add up to ${summary.massComputed.toFixed(
        1,
      )} kg at ${params.iron_dens} kg/m³. Check that iron_dens matches the run.`,
      zones: [],
    });
  }

  return warnings;
}

function aggregateBySpec(zones: Zone[], params: SolverParams, totalMass: number): SpecTotals[] {
  const stockKeys = new Set(params.stock.map(specKeyOf));
  const map = new Map<string, SpecTotals>();
  for (const z of zones) {
    let entry = map.get(z.specKey);
    if (!entry) {
      entry = {
        specKey: z.specKey,
        diameter: z.diameter,
        step: z.step,
        zones: 0,
        bars: 0,
        length: 0,
        lengthAnchored: 0,
        mass: 0,
        massAnchored: 0,
        massShare: 0,
        inStock: stockKeys.has(z.specKey),
      };
      map.set(z.specKey, entry);
    }
    entry.zones += 1;
    entry.bars += z.bars.length;
    entry.length += z.totalBarLength / 1000;
    entry.lengthAnchored += z.totalBarLengthAnchored / 1000;
    entry.mass += z.mass;
    entry.massAnchored += z.massAnchored;
  }
  const list = [...map.values()];
  for (const e of list) e.massShare = totalMass > 0 ? e.mass / totalMass : 0;
  return list.sort((a, b) => b.mass - a.mass);
}

/** Turn raw solver output into everything the UI renders. */
export function normalizeResult(raw: RawResult, override?: Partial<SolverParams>): NormalizedResult {
  const params: SolverParams = {
    ...DEFAULT_PARAMS,
    ...(raw.params ?? {}),
    ...(override ?? {}),
  };
  const direction: Axis = (override?.direction ?? raw.direction ?? raw.params?.direction ?? 'y') as Axis;
  params.direction = direction;

  const zones = (raw.zones ?? []).map((z, i) => normalizeZone(z, i, params));

  const massComputed = zones.reduce(
    (acc, z) => acc + barMass(z.diameter, z.totalBarLength, params.iron_dens),
    0,
  );
  const mass = typeof raw.mass === 'number' ? raw.mass : massComputed;
  const massAnchored = zones.reduce((acc, z) => acc + z.massAnchored, 0);
  const bars = zones.reduce((acc, z) => acc + z.bars.length, 0);
  const length = zones.reduce((acc, z) => acc + z.totalBarLength, 0) / 1000;
  const lengthAnchored = zones.reduce((acc, z) => acc + z.totalBarLengthAnchored, 0) / 1000;

  const bounds = boundsOf(zones);
  const coveredArea = unionArea(zones.map((z) => z.rect)) / 1e6;
  const boundingArea = ((bounds[2] - bounds[0]) * (bounds[3] - bounds[1])) / 1e6;

  const heaviest = [...zones].sort((a, b) => b.mass - a.mass);
  let running = 0;
  let paretoCount = 0;
  for (const z of heaviest) {
    if (running >= 0.8 * mass) break;
    running += z.mass;
    paretoCount += 1;
  }

  const summary: Summary = {
    zoneCount: zones.length,
    mass,
    massComputed,
    massDelta: mass > 0 ? Math.abs(mass - massComputed) / mass : 0,
    massAnchored,
    bars,
    length,
    lengthAnchored,
    coveredArea,
    boundingArea,
    massPerCoveredArea: coveredArea > 0 ? mass / coveredArea : 0,
    massPerBoundingArea: boundingArea > 0 ? mass / boundingArea : 0,
    bounds,
    bySpec: aggregateBySpec(zones, params, mass),
    heaviest,
    paretoCount,
    warnings: buildWarnings(zones, params, { mass, massComputed }),
  };

  return { raw, params, direction, zones, summary };
}
