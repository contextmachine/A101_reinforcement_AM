/**
 * Backend adapter — PROVISIONAL.
 *
 * The solver backend does not exist yet. Everything in this file is a
 * placeholder for it: the endpoint paths, the job/polling shape and the
 * response types below were chosen so the UI could be built, NOT agreed with
 * the backend. When the real service lands, align it here — this is the only
 * module that talks to the network, so nothing else has to change.
 *
 * What the UI needs from a backend, in whatever shape the backend prefers:
 *   1. hand it a drawing + the solver parameters, and get a handle back;
 *   2. ask whether that handle is finished yet;
 *   3. fetch the result JSON (the `RawResult` shape in lib/types.ts, which is
 *      the solver's own output format, verbatim);
 *   4. fetch the produced drawing files.
 *
 * Until then the app runs entirely on result files opened from disk, which
 * needs no backend at all.
 */
import type { ArtifactFormat, Job, RawResult, SolverParams } from '../lib/types';

/**
 * Base URL of the solver API, from `VITE_API_BASE`. Empty means same-origin,
 * which is what a reverse-proxied deployment or the dev proxy expects.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '');

/**
 * Whether a backend has been pointed at this build.
 *
 * Read from configuration rather than probed: there is no agreed health
 * endpoint to probe, and asking the dev server for one just gets the SPA's
 * index.html back with a 200, which would look like a healthy backend.
 */
export const isConfigured = (): boolean =>
  API_BASE.length > 0 || Boolean(import.meta.env.VITE_PROXY_TARGET);

const url = (path: string) => `${API_BASE}${path}`;

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function unwrap<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.error ?? body?.detail ?? detail;
    } catch {
      /* body was not JSON — keep the status text */
    }
    throw new ApiError(detail || `Request failed with ${res.status}`, res.status);
  }
  return (await res.json()) as T;
}

/** Upload a drawing and start a solver run. */
export async function createJob(
  file: File,
  params: SolverParams,
  signal?: AbortSignal,
): Promise<Job> {
  const form = new FormData();
  form.append('file', file, file.name);
  form.append('params', JSON.stringify(params));
  return unwrap<Job>(await fetch(url('/api/jobs'), { method: 'POST', body: form, signal }));
}

export async function getJob(jobId: string, signal?: AbortSignal): Promise<Job> {
  return unwrap<Job>(await fetch(url(`/api/jobs/${jobId}`), { signal }));
}

export async function getResult(jobId: string, signal?: AbortSignal): Promise<RawResult> {
  return unwrap<RawResult>(await fetch(url(`/api/jobs/${jobId}/result`), { signal }));
}

export function downloadUrl(jobId: string, format: ArtifactFormat): string {
  return url(`/api/jobs/${jobId}/download/${format}`);
}

export async function cancelJob(jobId: string): Promise<void> {
  await fetch(url(`/api/jobs/${jobId}`), { method: 'DELETE' });
}

/**
 * Poll a job until it reaches a terminal state.
 *
 * Backs off from 400 ms to 2 s so a long run does not hammer the API, and
 * reports every intermediate state through `onUpdate`. If the backend ends up
 * pushing progress over SSE or a websocket instead, replace this one function.
 */
export async function pollJob(
  jobId: string,
  onUpdate: (job: Job) => void,
  signal?: AbortSignal,
): Promise<Job> {
  let delay = 400;
  for (;;) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const job = await getJob(jobId, signal);
    onUpdate(job);
    if (job.status === 'done' || job.status === 'error') return job;
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 1.4, 2000);
  }
}
