import { useState } from 'react';
import type { NormalizedResult } from '../lib/types';
import { kg, num, pct, specLabel, sqm, tonnes } from '../lib/format';
import { downloadCsv } from '../lib/download';
import { MassByZoneChart, SpecBreakdownChart } from './Charts';

interface Props {
  result: NormalizedResult;
  colorScale: Map<string, string>;
  selected: number | null;
  onSelect: (i: number | null) => void;
  fileStem: string;
}

export default function SummaryPanel({ result, colorScale, selected, onSelect, fileStem }: Props) {
  const [metric, setMetric] = useState<'mass' | 'length' | 'bars'>('mass');
  const { summary, params } = result;

  const anchorageExtra = summary.massAnchored - summary.massComputed;

  const exportSpecCsv = () => {
    const header = [
      'diameter_mm',
      'step_mm',
      'zones',
      'bars',
      'length_m',
      'length_with_anchorage_m',
      'mass_kg',
      'mass_with_anchorage_kg',
      'share_of_total',
      'in_stock',
    ];
    const rows = summary.bySpec.map((s) => [
      s.diameter,
      s.step,
      s.zones,
      s.bars,
      s.length.toFixed(2),
      s.lengthAnchored.toFixed(2),
      s.mass.toFixed(2),
      s.massAnchored.toFixed(2),
      s.massShare.toFixed(5),
      s.inStock ? 'yes' : 'no',
    ]);
    downloadCsv([header, ...rows], `${fileStem}-rebar-summary.csv`);
  };

  return (
    <div className="stack">
      {summary.warnings.length > 0 && (
        <div className="stack" style={{ gap: 8 }}>
          {summary.warnings.map((w) => (
            <div key={w.title} className={`notice ${w.level === 'warn' ? 'warn' : ''}`}>
              <span aria-hidden>{w.level === 'warn' ? '⚠' : 'ℹ'}</span>
              <div className="body">
                <strong>{w.title}</strong>
                <p>{w.detail}</p>
                {w.zones.length > 0 && (
                  <p style={{ marginTop: 4 }}>
                    Zones:{' '}
                    {w.zones.slice(0, 14).map((i, k) => (
                      <span key={i}>
                        {k > 0 && ', '}
                        <a
                          href="#"
                          onClick={(e) => {
                            e.preventDefault();
                            onSelect(i);
                          }}
                        >
                          #{i + 1}
                        </a>
                      </span>
                    ))}
                    {w.zones.length > 14 && ` … +${w.zones.length - 14} more`}
                  </p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="kpi-grid">
        <Kpi
          label="Additional rebar"
          value={tonnes(summary.mass)}
          sub={`${kg(summary.mass, 0)} · ${summary.bars} bars`}
        />
        <Kpi
          label="Zones"
          value={num(summary.zoneCount)}
          sub={`${summary.paretoCount} carry 80% of the mass`}
        />
        <Kpi
          label="Rebar length"
          value={`${num(summary.length, 0)} m`}
          sub={`${num(summary.lengthAnchored, 0)} m with anchorage`}
        />
        <Kpi
          label="With anchorage"
          value={tonnes(summary.massAnchored)}
          sub={`+${kg(anchorageExtra, 0)} at ${params.anchor_k}·⌀`}
        />
        <Kpi
          label="Reinforced area"
          value={sqm(summary.coveredArea)}
          sub={`${pct(summary.coveredArea / (summary.boundingArea || 1))} of the ${sqm(
            summary.boundingArea,
          )} extents`}
        />
        <Kpi
          label="Intensity"
          value={`${num(summary.massPerCoveredArea, 1)} kg/m²`}
          sub={`${num(summary.massPerBoundingArea, 1)} kg/m² over the whole slab`}
        />
      </div>

      <div className="chart-grid">
        <MassByZoneChart
          zones={result.zones}
          totalMass={summary.mass}
          paretoCount={summary.paretoCount}
          selected={selected}
          onSelect={onSelect}
        />
        <SpecBreakdownChart
          specs={summary.bySpec}
          colorScale={colorScale}
          metric={metric}
          onMetricChange={setMetric}
        />
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Rebar schedule</h2>
          <span className="spacer" />
          <button className="sm" onClick={exportSpecCsv}>
            Export CSV
          </button>
        </div>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th className="left">Rebar</th>
                <th>Zones</th>
                <th>Bars</th>
                <th>Length, m</th>
                <th>+ anchorage, m</th>
                <th>Mass, kg</th>
                <th>Share</th>
                <th className="left">Stock</th>
              </tr>
            </thead>
            <tbody>
              {summary.bySpec.map((s) => (
                <tr key={s.specKey} style={{ cursor: 'default' }}>
                  <td className="left">
                    <span className="row">
                      <span className="swatch" style={{ background: colorScale.get(s.specKey) }} />
                      <span className="mono">{specLabel(s.diameter, s.step)}</span>
                    </span>
                  </td>
                  <td className="num">{num(s.zones)}</td>
                  <td className="num">{num(s.bars)}</td>
                  <td className="num">{num(s.length, 1)}</td>
                  <td className="num">{num(s.lengthAnchored, 1)}</td>
                  <td className="num">{num(s.mass, 1)}</td>
                  <td className="num">
                    <span className="bar-cell">
                      <span className="bar" style={{ width: `${s.massShare * 100}%` }} />
                      <span>{pct(s.massShare)}</span>
                    </span>
                  </td>
                  <td className="left">
                    {s.inStock ? (
                      <span className="badge ok">in stock</span>
                    ) : (
                      <span className="badge warn">off stock</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td className="left">Total</td>
                <td className="num">{num(summary.zoneCount)}</td>
                <td className="num">{num(summary.bars)}</td>
                <td className="num">{num(summary.length, 1)}</td>
                <td className="num">{num(summary.lengthAnchored, 1)}</td>
                <td className="num">{num(summary.mass, 1)}</td>
                <td className="num">100.0%</td>
                <td />
              </tr>
            </tfoot>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Run parameters</h2>
        </div>
        <div className="card-body">
          <dl
            style={{
              margin: 0,
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
              gap: 10,
            }}
          >
            <Param label="Direction" value={`along ${params.direction.toUpperCase()}`} />
            <Param
              label="Background mesh"
              value={specLabel(params.back_grid.diameter, params.back_grid.step)}
            />
            <Param label="Max layers" value={String(params.max_lay)} />
            <Param label="Min zone width" value={`${num(params.min_w)} mm`} />
            <Param label="Steel density" value={`${num(params.iron_dens)} kg/m³`} />
            <Param label="Anchorage" value={`${params.anchor_k} · ⌀`} />
            <Param
              label="Stock"
              value={params.stock.map((s) => specLabel(s.diameter, s.step)).join(', ') || '—'}
            />
            <Param
              label="Mass check"
              value={
                summary.massDelta < 1e-6
                  ? 'geometry matches reported mass'
                  : `${pct(summary.massDelta, 3)} off reported mass`
              }
            />
          </dl>
        </div>
      </div>
    </div>
  );
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function Param({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        {label}
      </dt>
      <dd className="mono" style={{ margin: '2px 0 0' }}>
        {value}
      </dd>
    </div>
  );
}
