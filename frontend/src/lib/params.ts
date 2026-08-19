import type { SolverParams } from './types';
import { specKeyOf } from './metrics';

/** File types the drawing input accepts. */
export const ACCEPT = '.dwg,.dxf';

/** Drop invalid and duplicate stock entries and sort them, so the payload is canonical. */
export function canonicalParams(params: SolverParams): SolverParams {
  const seen = new Set<string>();
  const stock = params.stock
    .filter((s) => Number.isFinite(s.diameter) && Number.isFinite(s.step) && s.diameter > 0 && s.step > 0)
    .filter((s) => {
      const key = specKeyOf(s);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((a, b) => a.diameter - b.diameter || a.step - b.step);
  return { ...params, stock };
}

/** Human-readable reasons the parameters cannot be submitted, most important first. */
export function validate(params: SolverParams, file: File | null): string[] {
  const problems: string[] = [];
  if (!file) problems.push('Choose a drawing to process.');
  if (canonicalParams(params).stock.length === 0) problems.push('Add at least one rebar option to the stock.');
  if (!(params.back_grid.diameter > 0) || !(params.back_grid.step > 0))
    problems.push('The background mesh needs a positive diameter and step.');
  if (!(params.max_lay >= 1)) problems.push('max_lay must be at least 1.');
  if (!(params.min_w > 0)) problems.push('min_w must be positive.');
  if (!(params.iron_dens > 0)) problems.push('iron_dens must be positive.');
  if (!(params.anchor_k >= 0)) problems.push('anchor_k cannot be negative.');
  return problems;
}
