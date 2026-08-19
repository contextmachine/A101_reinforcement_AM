const nf = (digits: number) =>
  new Intl.NumberFormat('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });

export const num = (v: number, digits = 0) =>
  Number.isFinite(v) ? nf(digits).format(v) : '—';

export const mm = (v: number) => `${num(v, 0)} mm`;
export const kg = (v: number, digits = 1) => `${num(v, digits)} kg`;
export const tonnes = (v: number) => `${num(v / 1000, 3)} t`;
export const metres = (v: number, digits = 1) => `${num(v, digits)} m`;
export const pct = (v: number, digits = 1) => `${num(v * 100, digits)}%`;
export const sqm = (v: number) => `${num(v, 1)} m²`;

export const specLabel = (diameter: number, step: number) => `⌀${diameter}/${step}`;

export function fileStem(name: string) {
  const base = name.replace(/\\/g, '/').split('/').pop() ?? name;
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(0, dot) : base;
}
