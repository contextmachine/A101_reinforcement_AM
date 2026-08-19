import { describe, expect, it } from 'vitest';
import sample from '../../../samples/TopY_arming.json';
import { DEFAULT_PARAMS, barMass, normalizeResult, unionArea } from '../metrics';
import type { RawResult, RectXYXY } from '../types';

const raw = sample as unknown as RawResult;

describe('unionArea', () => {
  it('is zero for no rectangles', () => {
    expect(unionArea([])).toBe(0);
  });

  it('counts overlap once', () => {
    const rects: RectXYXY[] = [
      [0, 0, 10, 10],
      [5, 5, 15, 15],
    ];
    // 100 + 100 - 25 overlap
    expect(unionArea(rects)).toBe(175);
  });

  it('collapses fully nested rectangles', () => {
    expect(unionArea([[0, 0, 10, 10], [2, 2, 4, 4]])).toBe(100);
  });
});

describe('barMass', () => {
  it('matches the ρ·πd²/4·L the solver uses', () => {
    // 1 m of ⌀25 at 7850 kg/m³ → 3.853 kg
    expect(barMass(25, 1000, 7850)).toBeCloseTo(3.8534, 3);
  });
});

describe('normalizeResult on the reference result', () => {
  const result = normalizeResult(raw);

  it('keeps every zone', () => {
    expect(result.zones).toHaveLength(raw.N);
    expect(result.summary.zoneCount).toBe(33);
  });

  it('reproduces the reported mass from the bar geometry', () => {
    // The solver's mass is exactly ρ·πd²/4·Σ(bar lengths) — no anchorage.
    expect(result.summary.massComputed).toBeCloseTo(raw.mass, 6);
    expect(result.summary.massDelta).toBeLessThan(1e-9);
  });

  it('totals the per-zone masses to the reported total', () => {
    const sum = result.zones.reduce((a, z) => a + z.mass, 0);
    expect(sum).toBeCloseTo(raw.mass, 6);
  });

  it('measures bar lengths from the segments rather than trusting `length`', () => {
    for (const z of result.zones) {
      expect(z.totalBarLength).toBeCloseTo(z.length * z.bars.length, 6);
      expect(z.bars.length).toBe(z.barsCount);
    }
  });

  it('adds anchorage at both ends of every bar', () => {
    const z = result.zones[0];
    const expected = z.totalBarLength + 2 * DEFAULT_PARAMS.anchor_k * z.diameter * z.bars.length;
    expect(z.totalBarLengthAnchored).toBeCloseTo(expected, 6);
    expect(z.massAnchored).toBeGreaterThan(z.mass);
  });

  it('splits the mass across the two rebar types used', () => {
    const specs = result.summary.bySpec;
    expect(specs.map((s) => s.specKey).sort()).toEqual(['d18@300', 'd25@150']);
    expect(specs.reduce((a, s) => a + s.mass, 0)).toBeCloseTo(raw.mass, 6);
    expect(specs.reduce((a, s) => a + s.massShare, 0)).toBeCloseTo(1, 9);
    expect(specs.every((s) => s.inStock)).toBe(true);
  });

  it('sorts specs by mass, descending — the colour slot order', () => {
    const masses = result.summary.bySpec.map((s) => s.mass);
    expect([...masses].sort((a, b) => b - a)).toEqual(masses);
  });

  it('derives the drawing extents from the geometry', () => {
    // Primary rectangles count towards the extents even though the viewer hides
    // them by default — they come from the load field, so they bound the slab.
    // The bars alone only reach x = 23400.
    expect(result.summary.bounds).toEqual([0, 0, 23600, 14000]);
    const barMaxX = Math.max(...result.zones.flatMap((z) => z.bars.map((b) => b[2])));
    expect(barMaxX).toBe(23400);
  });

  it('keeps the covered area inside the extents', () => {
    expect(result.summary.coveredArea).toBeGreaterThan(0);
    expect(result.summary.coveredArea).toBeLessThanOrEqual(result.summary.boundingArea + 1e-6);
  });

  it('reaches 80% of the mass with the Pareto count', () => {
    const top = result.summary.heaviest.slice(0, result.summary.paretoCount);
    expect(top.reduce((a, z) => a + z.mass, 0)).toBeGreaterThanOrEqual(0.8 * raw.mass);
    const oneFewer = top.slice(0, -1).reduce((a, z) => a + z.mass, 0);
    expect(oneFewer).toBeLessThan(0.8 * raw.mass);
  });

  it('flags the three zones with no primary rectangle', () => {
    const w = result.summary.warnings.find((x) => x.title.includes('without a primary'));
    expect(w?.zones).toHaveLength(3);
  });

  it('flags zones narrower than min_w', () => {
    const w = result.summary.warnings.find((x) => x.title.includes('min_w'));
    expect(w?.level).toBe('warn');
    expect(w?.zones.length).toBe(6);
  });

  it('warns when the result uses rebar outside the stock', () => {
    const narrowed = normalizeResult(raw, { stock: [{ diameter: 18, step: 300 }] });
    const w = narrowed.summary.warnings.find((x) => x.title.includes('outside the configured stock'));
    expect(w?.zones.length).toBeGreaterThan(0);
    expect(narrowed.summary.bySpec.find((s) => s.specKey === 'd25@150')?.inStock).toBe(false);
  });

  it('scales mass with the configured steel density', () => {
    const lighter = normalizeResult(raw, { iron_dens: 3925 });
    expect(lighter.summary.massComputed).toBeCloseTo(raw.mass / 2, 6);
    // The solver's own figure is reported as-is, so the mismatch is surfaced.
    expect(lighter.summary.warnings.some((w) => w.title.includes('Reported mass'))).toBe(true);
  });
});
