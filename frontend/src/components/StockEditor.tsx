import type { RebarSpec } from '../lib/types';
import { specKeyOf } from '../lib/metrics';

interface Props {
  value: RebarSpec[];
  onChange: (value: RebarSpec[]) => void;
  disabled?: boolean;
}

const COMMON_DIAMETERS = [8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 32];

export default function StockEditor({ value, onChange, disabled }: Props) {
  const seen = new Map<string, number>();
  value.forEach((s) => seen.set(specKeyOf(s), (seen.get(specKeyOf(s)) ?? 0) + 1));

  const update = (i: number, patch: Partial<RebarSpec>) =>
    onChange(value.map((s, k) => (k === i ? { ...s, ...patch } : s)));

  return (
    <div>
      <div className="stock-row faint" style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.04em' }}>
        <span />
        <span>⌀, mm</span>
        <span>Step, mm</span>
        <span />
      </div>
      {value.map((spec, i) => {
        const dup = (seen.get(specKeyOf(spec)) ?? 0) > 1;
        return (
          <div className="stock-row" key={i}>
            <span className="faint mono" style={{ fontSize: 11 }}>
              {i + 1}
            </span>
            <input
              type="number"
              min={1}
              step="any"
              list="rebar-diameters"
              value={Number.isFinite(spec.diameter) ? spec.diameter : ''}
              disabled={disabled}
              aria-label={`Diameter of stock item ${i + 1}`}
              style={dup ? { borderColor: 'var(--warn)' } : undefined}
              onChange={(e) => update(i, { diameter: e.target.valueAsNumber })}
            />
            <input
              type="number"
              min={1}
              step="any"
              value={Number.isFinite(spec.step) ? spec.step : ''}
              disabled={disabled}
              aria-label={`Step of stock item ${i + 1}`}
              style={dup ? { borderColor: 'var(--warn)' } : undefined}
              onChange={(e) => update(i, { step: e.target.valueAsNumber })}
            />
            <button
              className="ghost sm icon"
              disabled={disabled || value.length <= 1}
              aria-label={`Remove stock item ${i + 1}`}
              title="Remove"
              onClick={() => onChange(value.filter((_, k) => k !== i))}
            >
              ×
            </button>
          </div>
        );
      })}
      <datalist id="rebar-diameters">
        {COMMON_DIAMETERS.map((d) => (
          <option key={d} value={d} />
        ))}
      </datalist>
      <button
        className="sm"
        disabled={disabled}
        onClick={() => onChange([...value, { diameter: 20, step: 200 }])}
      >
        + Add option
      </button>
      {[...seen.values()].some((n) => n > 1) && (
        <div className="hint" style={{ color: 'var(--warn)' }}>
          Duplicate ⌀/step pairs are highlighted — they will be de-duplicated on submit.
        </div>
      )}
    </div>
  );
}
