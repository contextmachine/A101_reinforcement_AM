import { useRef, useState } from 'react';

interface Props {
  file: File | null;
  onFile: (file: File | null) => void;
  accept: string;
  disabled?: boolean;
}

const MB = 1024 * 1024;

export default function Dropzone({ file, onFile, accept, disabled }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const extensions = accept.split(',').map((e) => e.trim().toLowerCase());

  const take = (list: FileList | null) => {
    const f = list?.[0];
    if (!f) return;
    const ok = extensions.some((ext) => f.name.toLowerCase().endsWith(ext));
    if (!ok) {
      setError(`${f.name} is not one of ${extensions.join(', ')}`);
      return;
    }
    setError(null);
    onFile(f);
  };

  return (
    <div>
      <div
        className={`dropzone${over ? ' over' : ''}`}
        role="button"
        tabIndex={0}
        aria-disabled={disabled}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (!disabled && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          if (!disabled) take(e.dataTransfer.files);
        }}
      >
        {file ? (
          <>
            <div className="file">{file.name}</div>
            <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
              {(file.size / MB).toFixed(2)} MB · click to replace
            </div>
          </>
        ) : (
          <>
            <div className="big">Drop a drawing here</div>
            <div className="faint" style={{ fontSize: 12 }}>
              or click to browse · {extensions.join(' / ')}
            </div>
          </>
        )}
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          hidden
          onChange={(e) => {
            take(e.target.files);
            e.target.value = '';
          }}
        />
      </div>
      {error && (
        <div className="hint" style={{ color: 'var(--danger)' }}>
          {error}
        </div>
      )}
      {file && !disabled && (
        <button className="ghost sm" style={{ marginTop: 6 }} onClick={() => onFile(null)}>
          Remove
        </button>
      )}
    </div>
  );
}
