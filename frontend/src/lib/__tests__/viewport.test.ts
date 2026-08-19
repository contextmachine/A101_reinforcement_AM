import { describe, expect, it } from 'vitest';
import { fitBounds, niceStep, toScreenX, toScreenY, toWorldX, toWorldY, zoomAt } from '../viewport';
import type { RectXYXY } from '../types';

const bounds: RectXYXY = [0, 0, 23400, 14000];

describe('fitBounds', () => {
  const v = fitBounds(bounds, 800, 600, 20);

  it('centres on the bounds', () => {
    expect(v.cx).toBe(11700);
    expect(v.cy).toBe(7000);
  });

  it('fits the wider axis inside the margins', () => {
    expect(toScreenX(v, bounds[0])).toBeGreaterThanOrEqual(20 - 1e-6);
    expect(toScreenX(v, bounds[2])).toBeLessThanOrEqual(780 + 1e-6);
    expect(toScreenY(v, bounds[3])).toBeGreaterThanOrEqual(0);
  });

  it('puts the world centre at the canvas centre', () => {
    expect(toScreenX(v, 11700)).toBeCloseTo(400, 9);
    expect(toScreenY(v, 7000)).toBeCloseTo(300, 9);
  });

  it('flips Y — world Y up, screen Y down', () => {
    expect(toScreenY(v, 8000)).toBeLessThan(toScreenY(v, 6000));
  });
});

describe('round-tripping screen and world coordinates', () => {
  const v = fitBounds(bounds, 800, 600);
  it('is lossless', () => {
    expect(toWorldX(v, toScreenX(v, 1234))).toBeCloseTo(1234, 6);
    expect(toWorldY(v, toScreenY(v, 5678))).toBeCloseTo(5678, 6);
  });
});

describe('zoomAt', () => {
  const v = fitBounds(bounds, 800, 600);

  it('keeps the world point under the cursor pinned', () => {
    const before = { x: toWorldX(v, 210), y: toWorldY(v, 140) };
    const z = zoomAt(v, 2.5, 210, 140);
    expect(z.scale).toBeCloseTo(v.scale * 2.5, 9);
    expect(toWorldX(z, 210)).toBeCloseTo(before.x, 6);
    expect(toWorldY(z, 140)).toBeCloseTo(before.y, 6);
  });

  it('clamps at the maximum scale and leaves the viewport untouched', () => {
    const maxed = zoomAt(v, 1e9, 400, 300);
    expect(zoomAt(maxed, 10, 400, 300)).toBe(maxed);
  });
});

describe('niceStep', () => {
  it('returns a 1/2/5 × 10ⁿ step near the target pixel size', () => {
    const v = fitBounds(bounds, 800, 600);
    const step = niceStep(v, 100);
    expect(String(step / 10 ** Math.floor(Math.log10(step)))).toMatch(/^[125]$/);
    expect(step * v.scale).toBeGreaterThan(20);
    expect(step * v.scale).toBeLessThan(500);
  });
});
