import type { Job } from '../lib/types';

/** The pipeline the backend reports through `stage`. */
const STAGES = [
  { key: 'upload', label: 'Upload & convert' },
  { key: 'extract', label: 'Extract load mosaic' },
  { key: 'quantize', label: 'Quantise to a grid' },
  { key: 'optimize', label: 'Optimise zones' },
  { key: 'emit', label: 'Emit drawing & report' },
];

interface Props {
  job: Job;
  onCancel?: () => void;
}

export default function JobProgress({ job, onCancel }: Props) {
  const current = STAGES.findIndex((s) => s.key === job.stage);
  const indeterminate = job.status === 'running' && !(job.progress > 0);

  return (
    <div className="card" style={{ maxWidth: 460, margin: '48px auto' }}>
      <div className="card-head">
        <span className="spinner" />
        <h2>{job.status === 'queued' ? 'Queued' : 'Processing'}</h2>
        <span className="spacer" />
        <span className="badge mono">{job.jobId.slice(0, 8)}</span>
      </div>
      <div className="card-body">
        <div className="mono faint" style={{ fontSize: 12, marginBottom: 10, wordBreak: 'break-all' }}>
          {job.fileName}
        </div>
        <div className="progress-track">
          <div
            className={`progress-fill${indeterminate ? ' indeterminate' : ''}`}
            style={{ width: `${Math.round((job.progress || 0) * 100)}%` }}
          />
        </div>
        <ul className="stage-list">
          {STAGES.map((s, i) => (
            <li key={s.key} className={i === current ? 'active' : current > i ? 'done' : ''}>
              <span className="stage-dot" />
              {s.label}
            </li>
          ))}
        </ul>
        {onCancel && (
          <button className="ghost sm" style={{ marginTop: 14 }} onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
