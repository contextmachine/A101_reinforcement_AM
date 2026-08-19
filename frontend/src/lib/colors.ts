/**
 * Series colours come from CSS custom properties so that the light/dark values
 * live in one place — see the palette block in index.css. Canvas needs real
 * colour strings, so they are resolved from the document and cached per theme.
 */

const SLOTS = 8;

let cache: { theme: string; series: string[]; other: string } | null = null;

function readVar(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

const FALLBACK = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'];

function palette() {
  const theme = document.documentElement.dataset.theme ?? 'light';
  if (cache?.theme === theme) return cache;
  cache = {
    theme,
    series: Array.from({ length: SLOTS }, (_, i) => readVar(`--series-${i + 1}`, FALLBACK[i])),
    other: readVar('--series-other', '#6b7280'),
  };
  return cache;
}

/** Drop the cache after a theme switch so the next read picks up new values. */
export function invalidateColorCache() {
  cache = null;
}

/**
 * Colour for a categorical slot. Slots are assigned in fixed order and never
 * cycled — anything past the eighth slot gets the neutral "other" colour.
 */
export function seriesColor(slot: number): string {
  const p = palette();
  return slot >= 0 && slot < SLOTS ? p.series[slot] : p.other;
}

/**
 * Build a stable spec-key → colour map. The order of `keys` fixes the slots, so
 * pass them in a deterministic order (heaviest first) and keep it stable across
 * filter changes — colour follows the entity, never its current rank.
 */
export function buildColorScale(keys: string[]): Map<string, string> {
  const map = new Map<string, string>();
  keys.forEach((key, i) => map.set(key, seriesColor(i)));
  return map;
}

/** Any CSS colour → `rgba(...)` at the given alpha, for canvas fills. */
export function withAlpha(color: string, alpha: number): string {
  const hex = color.trim();
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex);
  if (m) {
    const h = m[1].length === 3 ? m[1].split('').map((c) => c + c).join('') : m[1];
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }
  return `color-mix(in srgb, ${color} ${Math.round(alpha * 100)}%, transparent)`;
}

/** Resolve a theme token (e.g. `--text-muted`) to a concrete colour. */
export function token(name: string, fallback = '#888'): string {
  return readVar(name, fallback);
}
