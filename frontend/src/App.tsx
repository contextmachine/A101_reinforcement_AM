import { useCallback, useMemo, useRef, useState } from 'react';
import ParamsForm from './components/ParamsForm';
import { canonicalParams, validate } from './lib/params';
import JobProgress from './components/JobProgress';
import PlanViewer from './components/PlanViewer';
import ZonesTable from './components/ZonesTable';
import SummaryPanel from './components/SummaryPanel';
import { API_BASE, ApiError, cancelJob, createJob, downloadUrl, getResult, isConfigured, pollJob } from './api/client';
import { DEFAULT_PARAMS, normalizeResult } from './lib/metrics';
import { buildColorScale } from './lib/colors';
import { downloadText, openDownload } from './lib/download';
import { fileStem } from './lib/format';
import { useTheme } from './lib/useTheme';
import type { ArtifactFormat, Job, NormalizedResult, RawResult, SolverParams } from './lib/types';

type Tab = 'viewer' | 'zones' | 'summary';

export default function App() {
  const { theme, toggle } = useTheme();
  const [file, setFile] = useState<File | null>(null);
  const [params, setParams] = useState<SolverParams>(DEFAULT_PARAMS);
  const [job, setJob] = useState<Job | null>(null);
  const [result, setResult] = useState<NormalizedResult | null>(null);
  const [sourceName, setSourceName] = useState<string>('result');
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('viewer');
  const [selected, setSelected] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const busy = job?.status === 'queued' || job?.status === 'running';
  const backendConfigured = isConfigured();

  /* ---------------------------------------------------------------- */
  /* Loading results                                                   */
  /* ---------------------------------------------------------------- */
  const applyRaw = useCallback(
    (raw: RawResult, name: string, override?: Partial<SolverParams>) => {
      if (!raw || !Array.isArray(raw.zones)) {
        setError('That file does not look like a solver result — no "zones" array found.');
        return;
      }
      setResult(normalizeResult(raw, override));
      setSourceName(name);
      setSelected(null);
      setError(null);
      setTab('viewer');
    },
    [],
  );

  const submit = useCallback(async () => {
    const clean = canonicalParams(params);
    const problems = validate(clean, file);
    if (problems.length > 0 || !file) {
      setError(problems[0] ?? 'Nothing to process.');
      return;
    }
    setError(null);
    setResult(null);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const started = await createJob(file, clean, ctrl.signal);
      setJob(started);
      const finished = await pollJob(started.jobId, setJob, ctrl.signal);
      if (finished.status === 'error') {
        setError(finished.error ?? 'The solver reported an error.');
        return;
      }
      const raw = await getResult(finished.jobId, ctrl.signal);
      applyRaw(raw, fileStem(file.name), clean);
    } catch (e) {
      if ((e as Error).name === 'AbortError') return;
      setError(
        e instanceof ApiError && e.status === 0
          ? 'Could not reach the solver API.'
          : (e as Error).message || 'Processing failed.',
      );
      setJob(null);
    } finally {
      abortRef.current = null;
    }
  }, [file, params, applyRaw]);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    if (job) void cancelJob(job.jobId);
    setJob(null);
  }, [job]);

  const loadSample = useCallback(async () => {
    try {
      const res = await fetch(`${import.meta.env.BASE_URL}sample-result.json`);
      if (!res.ok) throw new Error(`sample-result.json returned ${res.status}`);
      applyRaw(await res.json(), 'sample-TopY_arming', canonicalParams(params));
      setJob(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [applyRaw, params]);

  const loadJson = useCallback(
    async (f: File) => {
      try {
        applyRaw(JSON.parse(await f.text()), fileStem(f.name), canonicalParams(params));
        setJob(null);
      } catch {
        setError(`${f.name} is not valid JSON.`);
      }
    },
    [applyRaw, params],
  );

  /* ---------------------------------------------------------------- */
  /* Derived                                                           */
  /* ---------------------------------------------------------------- */
  // Slot order is fixed by mass rank at load time and never recomputed on
  // filtering, so a colour always means the same rebar type.
  const colorScale = useMemo(
    () => buildColorScale(result ? result.summary.bySpec.map((s) => s.specKey) : []),
    [result],
  );

  // The backend advertises what it can hand back; JSON always falls back to
  // re-serialising what is already in the browser, so it works offline too.
  const artifacts = job?.status === 'done' ? job.artifacts : undefined;

  const download = (format: ArtifactFormat) => {
    const href = artifacts?.[format];
    if (href) {
      openDownload(href.startsWith('http') ? href : `${API_BASE}${href}`);
      return;
    }
    if (format === 'json' && result) {
      downloadText(JSON.stringify(result.raw, null, 2), `${sourceName}.json`);
      return;
    }
    if (job?.status === 'done') openDownload(downloadUrl(job.jobId, format));
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden>
            <rect x="1.5" y="1.5" width="17" height="17" rx="2" fill="none" stroke="var(--accent)" strokeWidth="1.5" />
            <path d="M5 4v12M8 4v12M11 4v12M14 4v12" stroke="var(--accent)" strokeWidth="1.2" opacity="0.75" />
          </svg>
          A101 Reinforcement
          <small>additional arming</small>
        </div>
        <span className="spacer" />
        {!backendConfigured && (
          <span className="badge warn" title="No solver backend is configured for this build — result files can still be opened.">
            No backend
          </span>
        )}
        {result && (
          <>
            <span className="badge mono">{sourceName}</span>
            {(['dwg', 'dxf'] as const).map((fmt) => (
              <button
                key={fmt}
                className="sm"
                onClick={() => download(fmt)}
                disabled={!artifacts?.[fmt]}
                title={
                  artifacts?.[fmt]
                    ? `Download the result ${fmt.toUpperCase()}`
                    : `The backend did not produce a ${fmt.toUpperCase()} for this result`
                }
              >
                {fmt.toUpperCase()}
              </button>
            ))}
            <button className="sm" onClick={() => download('json')}>
              JSON
            </button>
          </>
        )}
        <button className="ghost sm" onClick={toggle} aria-label="Toggle colour theme" title="Toggle colour theme">
          {theme === 'dark' ? '☀' : '☾'}
        </button>
      </header>

      <div className="main">
        <aside className="sidebar">
          <ParamsForm
            file={file}
            onFile={setFile}
            params={params}
            onParams={setParams}
            busy={busy}
            apiReady={backendConfigured}
            onSubmit={submit}
            onLoadSample={loadSample}
            onLoadJson={loadJson}
          />
        </aside>

        <main className="content">
          {error && (
            <div style={{ padding: '12px 16px 0' }}>
              <div className="notice danger">
                <span aria-hidden>⚠</span>
                <div className="body">
                  <strong>Something went wrong</strong>
                  <p>{error}</p>
                </div>
                <button className="ghost sm" onClick={() => setError(null)} aria-label="Dismiss">
                  ×
                </button>
              </div>
            </div>
          )}

          {busy && job ? (
            <div className="content-scroll">
              <JobProgress job={job} onCancel={cancel} />
            </div>
          ) : result ? (
            <>
              <nav className="tabs" role="tablist">
                {(
                  [
                    ['viewer', 'Drawing'],
                    ['zones', `Zones (${result.zones.length})`],
                    ['summary', 'Summary'],
                  ] as [Tab, string][]
                ).map(([id, label]) => (
                  <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
                    {label}
                  </button>
                ))}
              </nav>

              {tab === 'viewer' && (
                <PlanViewer
                  result={result}
                  colorScale={colorScale}
                  selected={selected}
                  onSelect={setSelected}
                  themeKey={theme}
                />
              )}
              {tab === 'zones' && (
                <div className="content-scroll" style={{ display: 'flex', minHeight: 0 }}>
                  <ZonesTable
                    result={result}
                    colorScale={colorScale}
                    selected={selected}
                    onSelect={setSelected}
                    fileStem={sourceName}
                  />
                </div>
              )}
              {tab === 'summary' && (
                <div className="content-scroll">
                  <SummaryPanel
                    result={result}
                    colorScale={colorScale}
                    selected={selected}
                    onSelect={(i) => {
                      setSelected(i);
                      if (i !== null) setTab('viewer');
                    }}
                    fileStem={sourceName}
                  />
                </div>
              )}
            </>
          ) : (
            <div className="empty">
              <svg width="52" height="52" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2" aria-hidden>
                <rect x="2.5" y="2.5" width="19" height="19" rx="2" />
                <path d="M7 3v18M12 3v18M17 3v18" opacity="0.5" />
              </svg>
              <h2>No result loaded</h2>
              <p style={{ maxWidth: 400, margin: 0 }}>
                Open a result <code>.json</code> from the panel on the left to explore the drawing, the
                per-zone table and the analytics. Once the solver backend is available, a DWG or DXF
                dropped there can be processed directly.
              </p>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
