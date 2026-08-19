import { useMemo, useState } from 'react';
import type { SpecTotals, Zone } from '../lib/types';
import { num, specLabel } from '../lib/format';

/* ------------------------------------------------------------------ *
 * Shared bits
 * ------------------------------------------------------------------ */

interface TipState {
  x: number;
  y: number;
  title: string;
  rows: [string, string][];
}

function Tooltip({ tip, width }: { tip: TipState | null; width: number }) {
  if (!tip) return null;
  const flip = tip.x > width * 0.6;
  return (
    <div
      className="viewer-tooltip"
      style={{
        left: flip ? undefined : `${(tip.x / width) * 100}%`,
        right: flip ? `${100 - (tip.x / width) * 100}%` : undefined,
        top: tip.y,
        transform: flip ? 'translate(-10px, -50%)' : 'translate(10px, -50%)',
      }}
    >
      <div className="t">{tip.title}</div>
      <dl>
        {tip.rows.map(([k, v]) => (
          <div key={k} style={{ display: 'contents' }}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** Bar with rounded ends on the far side only, anchored to the baseline. */
function barPath(x: number, y: number, w: number, h: number, r: number, horizontal = false) {
  const rr = Math.max(0, Math.min(r, horizontal ? w : h, (horizontal ? h : w) / 2));
  if (horizontal) {
    return `M${x},${y} H${x + w - rr} a${rr},${rr} 0 0 1 ${rr},${rr} V${y + h - rr} a${rr},${rr} 0 0 1 ${-rr},${rr} H${x} Z`;
  }
  return `M${x},${y + h} V${y + rr} a${rr},${rr} 0 0 1 ${rr},${-rr} H${x + w - rr} a${rr},${rr} 0 0 1 ${rr},${rr} V${y + h} Z`;
}

/* ------------------------------------------------------------------ *
 * Mass by zone — one series, so a single sequential hue and no legend.
 * ------------------------------------------------------------------ */

export function MassByZoneChart({
  zones,
  totalMass,
  paretoCount,
  selected,
  onSelect,
}: {
  zones: Zone[];
  totalMass: number;
  paretoCount: number;
  selected: number | null;
  onSelect: (i: number | null) => void;
}) {
  const [tip, setTip] = useState<TipState | null>(null);
  const W = 720;
  const H = 240;
  const M = { top: 14, right: 12, bottom: 26, left: 52 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  const sorted = useMemo(() => [...zones].sort((a, b) => b.mass - a.mass), [zones]);
  const max = sorted.length ? sorted[0].mass : 1;
  const slot = plotW / Math.max(1, sorted.length);
  const barW = Math.max(1, slot - 2); // 2px surface gap between adjacent bars

  const ticks = useMemo(() => {
    const step = niceTick(max, 4);
    const out: number[] = [];
    for (let v = 0; v <= max * 1.0001; v += step) out.push(v);
    return out;
  }, [max]);

  // Cumulative share, precomputed so the render pass stays pure.
  const cumulative = useMemo(() => {
    const out: number[] = [];
    for (const z of sorted) out.push((out.at(-1) ?? 0) + z.mass);
    return out;
  }, [sorted]);

  return (
    <div className="card chart" style={{ position: 'relative' }}>
      <div className="card-head">
        <h2>Mass by zone</h2>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: 12 }}>
          {paretoCount} of {sorted.length} zones carry 80% of the mass
        </span>
      </div>
      <div className="card-body" style={{ position: 'relative' }}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Zone mass, descending" onMouseLeave={() => setTip(null)}>
          {ticks.map((t) => {
            const y = M.top + plotH - (t / max) * plotH;
            return (
              <g key={t}>
                <line className="axis" x1={M.left} x2={W - M.right} y1={y} y2={y} strokeWidth={t === 0 ? 1 : 0.5} />
                <text className="tick" x={M.left - 7} y={y + 3} textAnchor="end">
                  {num(t, 0)}
                </text>
              </g>
            );
          })}
          <text className="glabel" x={M.left - 44} y={M.top - 3}>
            kg
          </text>

          {sorted.map((z, i) => {
            const h = Math.max(1, (z.mass / max) * plotH);
            const x = M.left + i * slot + 1;
            const y = M.top + plotH - h;
            const isSel = selected === z.index;
            const inPareto = i < paretoCount;
            return (
              <path
                key={z.index}
                d={barPath(x, y, barW, h, 4)}
                fill="var(--series-1)"
                opacity={isSel ? 1 : inPareto ? 0.85 : 0.4}
                stroke={isSel ? 'var(--text)' : 'none'}
                strokeWidth={isSel ? 1.5 : 0}
                style={{ cursor: 'pointer' }}
                onClick={() => onSelect(selected === z.index ? null : z.index)}
                onMouseMove={() =>
                  setTip({
                    x: x + barW / 2,
                    y: Math.max(30, (y / H) * 100 * 2.2),
                    title: `Zone #${z.index + 1} · ${specLabel(z.diameter, z.step)}`,
                    rows: [
                      ['Mass', `${num(z.mass, 1)} kg`],
                      ['Share', `${num((z.mass / totalMass) * 100, 1)}%`],
                      ['Cumulative', `${num((cumulative[i] / totalMass) * 100, 1)}%`],
                      ['Bars', num(z.barsCount)],
                    ],
                  })
                }
              />
            );
          })}

          {paretoCount > 0 && paretoCount < sorted.length && (
            <line
              x1={M.left + paretoCount * slot}
              x2={M.left + paretoCount * slot}
              y1={M.top}
              y2={M.top + plotH}
              stroke="var(--text-faint)"
              strokeWidth={1}
              strokeDasharray="3 3"
            />
          )}

          <text className="glabel" x={M.left} y={H - 8}>
            zones, heaviest first
          </text>
        </svg>
        <Tooltip tip={tip} width={W} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Breakdown by rebar spec — categorical identity, direct-labelled.
 * ------------------------------------------------------------------ */

export function SpecBreakdownChart({
  specs,
  colorScale,
  metric,
  onMetricChange,
}: {
  specs: SpecTotals[];
  colorScale: Map<string, string>;
  metric: 'mass' | 'length' | 'bars';
  onMetricChange: (m: 'mass' | 'length' | 'bars') => void;
}) {
  const [tip, setTip] = useState<TipState | null>(null);
  const W = 720;
  const rowH = 34;
  const M = { top: 10, right: 90, bottom: 22, left: 96 };
  const H = M.top + M.bottom + specs.length * rowH;
  const plotW = W - M.left - M.right;

  const valueOf = (s: SpecTotals) => (metric === 'mass' ? s.mass : metric === 'length' ? s.length : s.bars);
  const unit = metric === 'mass' ? 'kg' : metric === 'length' ? 'm' : 'bars';
  const max = Math.max(1, ...specs.map(valueOf));

  return (
    <div className="card chart" style={{ position: 'relative' }}>
      <div className="card-head">
        <h2>By rebar type</h2>
        <span className="spacer" />
        <div className="seg">
          {(['mass', 'length', 'bars'] as const).map((m) => (
            <button key={m} className="sm" aria-pressed={metric === m} onClick={() => onMetricChange(m)}>
              {m === 'mass' ? 'Mass' : m === 'length' ? 'Length' : 'Bars'}
            </button>
          ))}
        </div>
      </div>
      <div className="card-body" style={{ position: 'relative' }}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Reinforcement ${metric} by rebar type`} onMouseLeave={() => setTip(null)}>
          {specs.map((s, i) => {
            const v = valueOf(s);
            const w = Math.max(2, (v / max) * plotW);
            const y = M.top + i * rowH + 4;
            const h = rowH - 12; // leaves a 2px+ surface gap between adjacent bars
            return (
              <g key={s.specKey}>
                <text className="tick" x={M.left - 10} y={y + h / 2 + 4} textAnchor="end" style={{ fontSize: 12 }}>
                  {specLabel(s.diameter, s.step)}
                </text>
                <path
                  d={barPath(M.left, y, w, h, 4, true)}
                  fill={colorScale.get(s.specKey) ?? 'var(--series-other)'}
                  style={{ cursor: 'default' }}
                  onMouseMove={() =>
                    setTip({
                      x: M.left + w,
                      y: y + h / 2,
                      title: specLabel(s.diameter, s.step),
                      rows: [
                        ['Zones', num(s.zones)],
                        ['Bars', num(s.bars)],
                        ['Length', `${num(s.length, 1)} m`],
                        ['+ anchorage', `${num(s.lengthAnchored, 1)} m`],
                        ['Mass', `${num(s.mass, 1)} kg`],
                        ['Share', `${num(s.massShare * 100, 1)}%`],
                      ],
                    })
                  }
                />
                {/* Direct label — identity and value never rest on colour alone. */}
                <text
                  className="tick"
                  x={M.left + w + 8}
                  y={y + h / 2 + 4}
                  style={{ fontSize: 12, fill: 'var(--text)' }}
                >
                  {num(v, metric === 'bars' ? 0 : metric === 'mass' ? 0 : 1)} {unit}
                </text>
                {!s.inStock && (
                  <text className="tick" x={M.left + 8} y={y + h / 2 + 4} style={{ fill: 'var(--warn)', fontSize: 11 }}>
                    not in stock
                  </text>
                )}
              </g>
            );
          })}
          <line className="axis" x1={M.left} x2={M.left} y1={M.top} y2={H - M.bottom} />
        </svg>
        <Tooltip tip={tip} width={W} />
      </div>
    </div>
  );
}

function niceTick(max: number, count: number) {
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  const norm = raw / mag;
  return (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
}
