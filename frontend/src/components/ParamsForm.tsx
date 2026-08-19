import type { Axis, SolverParams } from '../lib/types';
import { DEFAULT_PARAMS } from '../lib/metrics';
import { ACCEPT, validate } from '../lib/params';
import Dropzone from './Dropzone';
import StockEditor from './StockEditor';

interface Props {
  file: File | null;
  onFile: (f: File | null) => void;
  params: SolverParams;
  onParams: (p: SolverParams) => void;
  busy: boolean;
  /** Whether a solver backend is configured for this build. */
  apiReady: boolean;
  onSubmit: () => void;
  onLoadSample: () => void;
  onLoadJson: (file: File) => void;
}

export default function ParamsForm({
  file,
  onFile,
  params,
  onParams,
  busy,
  apiReady,
  onSubmit,
  onLoadSample,
  onLoadJson,
}: Props) {
  const set = <K extends keyof SolverParams>(key: K, value: SolverParams[K]) =>
    onParams({ ...params, [key]: value });

  const problems = validate(params, file);

  return (
    <>
      <div>
        <div className="section-title">Drawing</div>
        <Dropzone file={file} onFile={onFile} accept={ACCEPT} disabled={busy} />
        <div className="hint">
          The slab plan with the <code>KLEENKA</code> load mosaic. DWG is converted server-side; DXF is
          read directly.
        </div>
      </div>

      <div>
        <div className="section-title">Bar direction</div>
        <div className="seg" role="group" aria-label="Bar direction">
          {(['y', 'x'] as Axis[]).map((axis) => (
            <button
              key={axis}
              aria-pressed={params.direction === axis}
              disabled={busy}
              onClick={() => set('direction', axis)}
            >
              Along {axis.toUpperCase()}
            </button>
          ))}
        </div>
        <div className="hint">Additional bars run parallel to this axis; zones are laid out across it.</div>
      </div>

      <div>
        <div className="section-title">Background mesh</div>
        <div className="field-grid">
          <label className="field">
            <span>
              Diameter <code>mm</code>
            </span>
            <input
              type="number"
              min={1}
              value={params.back_grid.diameter}
              disabled={busy}
              onChange={(e) =>
                set('back_grid', { ...params.back_grid, diameter: e.target.valueAsNumber })
              }
            />
          </label>
          <label className="field">
            <span>
              Step <code>mm</code>
            </span>
            <input
              type="number"
              min={1}
              step="any"
              value={params.back_grid.step}
              disabled={busy}
              onChange={(e) => set('back_grid', { ...params.back_grid, step: e.target.valueAsNumber })}
            />
          </label>
        </div>
        <div className="hint">
          <code>back_grid</code> — the mesh already in the slab. Additional reinforcement is what the
          solver adds on top of it.
        </div>
      </div>

      <div>
        <div className="section-title">Additional rebar stock</div>
        <StockEditor value={params.stock} onChange={(stock) => set('stock', stock)} disabled={busy} />
        <div className="hint">
          <code>stock</code> — the ⌀/step pairs the solver may choose from.
        </div>
      </div>

      <div>
        <div className="section-title">Constraints</div>
        <div className="field-grid">
          <label className="field">
            <span>
              Max layers <code>max_lay</code>
            </span>
            <input
              type="number"
              min={1}
              max={10}
              value={params.max_lay}
              disabled={busy}
              onChange={(e) => set('max_lay', e.target.valueAsNumber)}
            />
          </label>
          <label className="field">
            <span>
              Min width <code>min_w</code>
            </span>
            <input
              type="number"
              min={1}
              step="any"
              value={params.min_w}
              disabled={busy}
              onChange={(e) => set('min_w', e.target.valueAsNumber)}
            />
          </label>
          <label className="field">
            <span>
              Density <code>iron_dens</code>
            </span>
            <input
              type="number"
              min={1}
              step="any"
              value={params.iron_dens}
              disabled={busy}
              onChange={(e) => set('iron_dens', e.target.valueAsNumber)}
            />
          </label>
          <label className="field">
            <span>
              Anchorage <code>anchor_k</code>
            </span>
            <input
              type="number"
              min={0}
              step="any"
              value={params.anchor_k}
              disabled={busy}
              onChange={(e) => set('anchor_k', e.target.valueAsNumber)}
            />
          </label>
        </div>
        <div className="hint">
          Anchorage length is <code>anchor_k · ⌀</code> at each bar end — it is reported alongside the
          net mass, never folded into it.
        </div>
      </div>

      <div className="stack" style={{ gap: 8 }}>
        <button
          className="primary"
          disabled={busy || problems.length > 0 || !apiReady}
          onClick={onSubmit}
          title={apiReady ? undefined : 'No solver backend is configured for this build'}
        >
          {busy ? 'Processing…' : 'Process drawing'}
        </button>
        {!apiReady ? (
          <div className="notice warn" style={{ padding: '8px 10px' }}>
            <span aria-hidden>⚠</span>
            <div className="body">
              <strong>No solver backend yet</strong>
              <p>
                Processing needs the solver service. Until it is available, open a result{' '}
                <code>.json</code> from a previous run below — the viewer, the zone table and the
                analytics all work from that file alone.
              </p>
            </div>
          </div>
        ) : (
          problems.length > 0 && !busy && <div className="hint">{problems[0]}</div>
        )}

        <div className="section-title" style={{ marginTop: 8 }}>
          Open a result
        </div>
        <label>
          <input
            type="file"
            accept=".json"
            hidden
            disabled={busy}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onLoadJson(f);
              e.target.value = '';
            }}
          />
          <span className="dropzone" style={{ display: 'block', padding: '14px 12px' }}>
            <span className="big">Open a result .json</span>
            <span className="faint" style={{ fontSize: 12, display: 'block' }}>
              the solver's output file, inspected without re-processing
            </span>
          </span>
        </label>

        <div className="row" style={{ justifyContent: 'space-between' }}>
          <button className="ghost sm" disabled={busy} onClick={() => onParams(DEFAULT_PARAMS)}>
            Reset parameters
          </button>
          <button className="ghost sm" disabled={busy} onClick={onLoadSample}>
            Load sample result
          </button>
        </div>
      </div>
    </>
  );
}
