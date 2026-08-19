import { useEffect, useMemo, useRef, useState } from 'react';
import type { NormalizedResult, Zone } from '../lib/types';
import { downloadCsv } from '../lib/download';
import { num, specLabel } from '../lib/format';

interface Props {
  result: NormalizedResult;
  colorScale: Map<string, string>;
  selected: number | null;
  onSelect: (index: number | null) => void;
  fileStem: string;
}

type ColumnId =
  | 'index'
  | 'spec'
  | 'x'
  | 'y'
  | 'width'
  | 'length'
  | 'bars'
  | 'rebar'
  | 'anchored'
  | 'mass'
  | 'share'
  | 'density';

interface Column {
  id: ColumnId;
  label: string;
  title: string;
  align?: 'left';
  value: (z: Zone) => number;
  render: (z: Zone, ctx: { total: number }) => React.ReactNode;
  /** Column footer, when a total makes sense. */
  total?: (zones: Zone[]) => React.ReactNode;
}

export default function ZonesTable({ result, colorScale, selected, onSelect, fileStem }: Props) {
  const [sort, setSort] = useState<{ id: ColumnId; dir: 1 | -1 }>({ id: 'mass', dir: -1 });
  const [specFilter, setSpecFilter] = useState<string>('all');
  const bodyRef = useRef<HTMLTableSectionElement>(null);

  const totalMass = result.summary.mass;

  const columns: Column[] = useMemo(
    () => [
      {
        id: 'index',
        label: '#',
        title: 'Zone number, in the order the solver emitted them',
        align: 'left',
        value: (z) => z.index,
        render: (z) => (
          <span className="row">
            <span className="swatch" style={{ background: colorScale.get(z.specKey) }} />
            {z.index + 1}
          </span>
        ),
        total: (zs) => `${zs.length} zones`,
      },
      {
        id: 'spec',
        label: 'Rebar',
        title: 'Diameter / spacing, mm',
        align: 'left',
        value: (z) => z.diameter * 1000 + z.step,
        render: (z) => <span className="mono">{specLabel(z.diameter, z.step)}</span>,
      },
      {
        id: 'x',
        label: 'X range',
        title: 'Extent of the final rectangle along X, mm',
        value: (z) => z.rect[0],
        render: (z) => (
          <span className="mono faint">
            {num(z.rect[0])}–{num(z.rect[2])}
          </span>
        ),
      },
      {
        id: 'y',
        label: 'Y range',
        title: 'Extent of the final rectangle along Y, mm',
        value: (z) => z.rect[1],
        render: (z) => (
          <span className="mono faint">
            {num(z.rect[1])}–{num(z.rect[3])}
          </span>
        ),
      },
      {
        id: 'width',
        label: 'Width',
        title: 'Zone extent across the bars, mm',
        value: (z) => z.width,
        render: (z) => num(z.width),
      },
      {
        id: 'length',
        label: 'Bar len',
        title: 'Length of a single bar, mm',
        value: (z) => z.length,
        render: (z) => num(z.length),
      },
      {
        id: 'bars',
        label: 'Bars',
        title: 'Number of bars in the zone',
        value: (z) => z.barsCount,
        render: (z) => num(z.barsCount),
        total: (zs) => num(zs.reduce((a, z) => a + z.barsCount, 0)),
      },
      {
        id: 'rebar',
        label: 'Rebar, m',
        title: 'Σ of bar lengths, metres',
        value: (z) => z.totalBarLength,
        render: (z) => num(z.totalBarLength / 1000, 1),
        total: (zs) => num(zs.reduce((a, z) => a + z.totalBarLength, 0) / 1000, 1),
      },
      {
        id: 'anchored',
        label: '+anch, m',
        title: 'Σ of bar lengths including 2 · anchor_k · ⌀ per bar, metres',
        value: (z) => z.totalBarLengthAnchored,
        render: (z) => num(z.totalBarLengthAnchored / 1000, 1),
        total: (zs) => num(zs.reduce((a, z) => a + z.totalBarLengthAnchored, 0) / 1000, 1),
      },
      {
        id: 'mass',
        label: 'Mass, kg',
        title: 'Zone mass as reported by the solver',
        value: (z) => z.mass,
        render: (z) => num(z.mass, 1),
        total: (zs) => num(zs.reduce((a, z) => a + z.mass, 0), 1),
      },
      {
        id: 'share',
        label: 'Share',
        title: 'Share of the total additional-reinforcement mass',
        value: (z) => z.mass,
        render: (z, ctx) => {
          const share = ctx.total > 0 ? z.mass / ctx.total : 0;
          return (
            <span className="bar-cell">
              <span className="bar" style={{ width: `${Math.max(1, share * 100)}%` }} />
              <span>{num(share * 100, 1)}%</span>
            </span>
          );
        },
      },
      {
        id: 'density',
        label: 'kg/m²',
        title: 'Zone mass per square metre of the zone rectangle',
        value: (z) => z.massPerArea,
        render: (z) => num(z.massPerArea, 1),
      },
    ],
    [colorScale],
  );

  const filtered = useMemo(
    () => (specFilter === 'all' ? result.zones : result.zones.filter((z) => z.specKey === specFilter)),
    [result.zones, specFilter],
  );

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.id === sort.id) ?? columns[0];
    return [...filtered].sort((a, b) => (col.value(a) - col.value(b)) * sort.dir || a.index - b.index);
  }, [filtered, sort, columns]);

  // Keep a selection made in the viewer visible in the table.
  useEffect(() => {
    if (selected === null || !bodyRef.current) return;
    const row = bodyRef.current.querySelector<HTMLElement>(`[data-zone="${selected}"]`);
    row?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  const toggleSort = (id: ColumnId) =>
    setSort((prev) => (prev.id === id ? { id, dir: prev.dir === 1 ? -1 : 1 } : { id, dir: id === 'index' ? 1 : -1 }));

  const exportCsv = () => {
    const header = [
      'zone',
      'diameter_mm',
      'step_mm',
      'x1_mm',
      'y1_mm',
      'x2_mm',
      'y2_mm',
      'width_mm',
      'bar_length_mm',
      'bars_count',
      'rebar_length_m',
      'rebar_length_with_anchorage_m',
      'mass_kg',
      'mass_with_anchorage_kg',
      'share_of_total',
      'kg_per_m2',
      'has_primary_rectangle',
    ];
    const rows = sorted.map((z) => [
      z.index + 1,
      z.diameter,
      z.step,
      z.rect[0],
      z.rect[1],
      z.rect[2],
      z.rect[3],
      z.width,
      z.length,
      z.barsCount,
      (z.totalBarLength / 1000).toFixed(3),
      (z.totalBarLengthAnchored / 1000).toFixed(3),
      z.mass.toFixed(3),
      z.massAnchored.toFixed(3),
      totalMass > 0 ? (z.mass / totalMass).toFixed(5) : '0',
      z.massPerArea.toFixed(3),
      z.primaryRect ? 'yes' : 'no',
    ]);
    downloadCsv([header, ...rows], `${fileStem}-zones.csv`);
  };

  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      <div className="card-head">
        <h2>Zones</h2>
        <span className="badge">{sorted.length} of {result.zones.length}</span>
        <span className="spacer" />
        <select
          value={specFilter}
          onChange={(e) => setSpecFilter(e.target.value)}
          style={{ width: 'auto' }}
          aria-label="Filter by rebar"
        >
          <option value="all">All rebar</option>
          {result.summary.bySpec.map((s) => (
            <option key={s.specKey} value={s.specKey}>
              {specLabel(s.diameter, s.step)} ({s.zones})
            </option>
          ))}
        </select>
        <button className="sm" onClick={exportCsv}>
          Export CSV
        </button>
      </div>

      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  key={c.id}
                  className={c.align === 'left' ? 'left' : undefined}
                  title={c.title}
                  onClick={() => toggleSort(c.id)}
                  aria-sort={sort.id === c.id ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}
                >
                  {c.label}
                  {sort.id === c.id && <span className="arrow">{sort.dir === 1 ? '▲' : '▼'}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody ref={bodyRef}>
            {sorted.map((z) => (
              <tr
                key={z.index}
                data-zone={z.index}
                className={selected === z.index ? 'selected' : undefined}
                onClick={() => onSelect(selected === z.index ? null : z.index)}
              >
                {columns.map((c) => (
                  <td key={c.id} className={c.align === 'left' ? 'left' : 'num'}>
                    {c.render(z, { total: totalMass })}
                  </td>
                ))}
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr>
                <td className="left faint" colSpan={columns.length}>
                  No zones match this filter.
                </td>
              </tr>
            )}
          </tbody>
          <tfoot>
            <tr>
              {columns.map((c) => (
                <td key={c.id} className={c.align === 'left' ? 'left' : 'num'}>
                  {c.total ? c.total(sorted) : ''}
                </td>
              ))}
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}
