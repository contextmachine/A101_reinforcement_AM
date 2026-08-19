import type { RectXYXY } from './types';

/**
 * World→screen transform for the plan viewer.
 *
 * World units are millimetres with Y pointing up (CAD convention); screen units
 * are CSS pixels with Y pointing down, hence the sign flip on `y`.
 */
export interface Viewport {
  /** Pixels per millimetre. */
  scale: number;
  /** World coordinate at the centre of the canvas. */
  cx: number;
  cy: number;
  /** Canvas size in CSS pixels. */
  width: number;
  height: number;
}

export const toScreenX = (v: Viewport, x: number) => (x - v.cx) * v.scale + v.width / 2;
export const toScreenY = (v: Viewport, y: number) => v.height / 2 - (y - v.cy) * v.scale;
export const toWorldX = (v: Viewport, sx: number) => (sx - v.width / 2) / v.scale + v.cx;
export const toWorldY = (v: Viewport, sy: number) => (v.height / 2 - sy) / v.scale + v.cy;

export const MIN_SCALE = 1e-4;
export const MAX_SCALE = 2;

export function clampScale(scale: number) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

/** Viewport that fits `bounds` into `width`×`height` with a pixel margin. */
export function fitBounds(
  bounds: RectXYXY,
  width: number,
  height: number,
  margin = 32,
): Viewport {
  const w = Math.max(1, bounds[2] - bounds[0]);
  const h = Math.max(1, bounds[3] - bounds[1]);
  const usableW = Math.max(1, width - margin * 2);
  const usableH = Math.max(1, height - margin * 2);
  return {
    scale: clampScale(Math.min(usableW / w, usableH / h)),
    cx: (bounds[0] + bounds[2]) / 2,
    cy: (bounds[1] + bounds[3]) / 2,
    width,
    height,
  };
}

/** Zoom by `factor`, keeping the world point under (sx, sy) pinned. */
export function zoomAt(v: Viewport, factor: number, sx: number, sy: number): Viewport {
  const scale = clampScale(v.scale * factor);
  if (scale === v.scale) return v;
  const wx = toWorldX(v, sx);
  const wy = toWorldY(v, sy);
  // Solve for the centre that keeps (wx, wy) under (sx, sy) at the new scale.
  return {
    ...v,
    scale,
    cx: wx - (sx - v.width / 2) / scale,
    cy: wy + (sy - v.height / 2) / scale,
  };
}

/**
 * A "nice" round world length that renders somewhere near `targetPx` pixels —
 * used for the scale bar and the background grid.
 */
export function niceStep(v: Viewport, targetPx: number): number {
  const raw = targetPx / v.scale;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = norm >= 5 ? 5 : norm >= 2 ? 2 : 1;
  return step * mag;
}
