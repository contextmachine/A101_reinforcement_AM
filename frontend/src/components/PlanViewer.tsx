import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { NormalizedResult, RectXYXY, Zone } from '../lib/types';
import { token, withAlpha } from '../lib/colors';
import { mm, kg, num, specLabel } from '../lib/format';
import {
  fitBounds,
  niceStep,
  toScreenX,
  toScreenY,
  toWorldX,
  toWorldY,
  zoomAt,
  type Viewport,
} from '../lib/viewport';

interface Props {
  result: NormalizedResult;
  colorScale: Map<string, string>;
  selected: number | null;
  onSelect: (index: number | null) => void;
  /** Bumped by the parent to force a re-read of theme colours. */
  themeKey: string;
}

interface Hover {
  zone: Zone;
  sx: number;
  sy: number;
}

const PAD = 24;

export default function PlanViewer({ result, colorScale, selected, onSelect, themeKey }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [view, setView] = useState<Viewport | null>(null);
  const [hover, setHover] = useState<Hover | null>(null);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [panning, setPanning] = useState(false);
  const [showPrimary, setShowPrimary] = useState(false);
  const [showBars, setShowBars] = useState(true);
  const [showLabels, setShowLabels] = useState(true);
  const [showGrid, setShowGrid] = useState(true);
  const [hiddenSpecs, setHiddenSpecs] = useState<Set<string>>(() => new Set());

  const bounds = result.summary.bounds;

  /* ---------------------------------------------------------------- */
  /* Sizing                                                            */
  /* ---------------------------------------------------------------- */
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ width: Math.round(width), height: Math.round(height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const fit = useCallback(() => {
    if (size.width > 0 && size.height > 0) {
      setView(fitBounds(bounds, size.width, size.height, PAD));
    }
  }, [bounds, size.width, size.height]);

  /*
   * Camera state is adjusted during render rather than in an effect — React's
   * documented "adjusting state when props change" pattern. It avoids the extra
   * commit an effect would cause, which on a canvas shows up as a visible frame
   * at the wrong zoom.
   */
  const [prevResult, setPrevResult] = useState(result);
  if (prevResult !== result) {
    setPrevResult(result);
    setView(null);
    setHiddenSpecs(new Set());
    setHover(null);
  }

  const [prevSize, setPrevSize] = useState(size);
  if (prevSize !== size) {
    setPrevSize(size);
    if (size.width > 0 && size.height > 0) {
      setView((prev) =>
        prev
          ? { ...prev, width: size.width, height: size.height }
          : fitBounds(bounds, size.width, size.height, PAD),
      );
    }
  }

  // A result loaded before the canvas has been measured needs fitting once the
  // first measurement lands. If a zone was already selected elsewhere — the tab
  // was switched from a summary link, which remounts this component — open on
  // that zone rather than on the whole slab.
  if (view === null && size.width > 0 && size.height > 0) {
    const preselected = selected === null ? null : result.zones[selected];
    setView(
      preselected
        ? zoneViewport(preselected, size.width, size.height)
        : fitBounds(bounds, size.width, size.height, PAD),
    );
  }

  /* ---------------------------------------------------------------- */
  /* Visible zones                                                     */
  /* ---------------------------------------------------------------- */
  const visibleZones = useMemo(
    () => result.zones.filter((z) => !hiddenSpecs.has(z.specKey)),
    [result.zones, hiddenSpecs],
  );

  const specs = result.summary.bySpec;

  /* ---------------------------------------------------------------- */
  /* Drawing                                                           */
  /* ---------------------------------------------------------------- */
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !view || view.width === 0 || view.height === 0) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(view.width * dpr);
    canvas.height = Math.round(view.height * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, view.width, view.height);

    const colText = token('--text', '#111');
    const colMuted = token('--text-muted', '#666');
    const colFaint = token('--text-faint', '#999');
    const colGrid = token('--grid', '#e5e5e5');
    const colBorder = token('--border-strong', '#bbb');
    const colSurface = token('--canvas-bg', '#fff');

    /* Background grid ------------------------------------------------ */
    if (showGrid) {
      const step = niceStep(view, 90);
      ctx.strokeStyle = colGrid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      const x0 = Math.floor(toWorldX(view, 0) / step) * step;
      const x1 = toWorldX(view, view.width);
      for (let x = x0; x <= x1; x += step) {
        const sx = Math.round(toScreenX(view, x)) + 0.5;
        ctx.moveTo(sx, 0);
        ctx.lineTo(sx, view.height);
      }
      const y0 = Math.floor(toWorldY(view, view.height) / step) * step;
      const y1 = toWorldY(view, 0);
      for (let y = y0; y <= y1; y += step) {
        const sy = Math.round(toScreenY(view, y)) + 0.5;
        ctx.moveTo(0, sy);
        ctx.lineTo(view.width, sy);
      }
      ctx.stroke();
    }

    /* Slab outline --------------------------------------------------- */
    ctx.strokeStyle = colBorder;
    ctx.setLineDash([]);
    ctx.lineWidth = 1.5;
    strokeRect(ctx, view, bounds);

    /* Primary (pre-quantisation) rectangles -------------------------- */
    if (showPrimary) {
      ctx.strokeStyle = colFaint;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      for (const z of visibleZones) {
        if (z.primaryRect) strokeRect(ctx, view, z.primaryRect);
      }
      ctx.setLineDash([]);
    }

    /* Zone fills ----------------------------------------------------- */
    for (const z of visibleZones) {
      const color = colorScale.get(z.specKey) ?? colMuted;
      const isSel = selected === z.index;
      const [sx, sy, sw, sh] = screenRect(view, z.rect);
      ctx.fillStyle = withAlpha(color, isSel ? 0.28 : 0.13);
      ctx.fillRect(sx, sy, sw, sh);
      ctx.strokeStyle = color;
      ctx.lineWidth = isSel ? 2.5 : 1.25;
      ctx.strokeRect(sx, sy, sw, sh);
    }

    /* Bars ----------------------------------------------------------- */
    if (showBars) {
      for (const z of visibleZones) {
        const color = colorScale.get(z.specKey) ?? colMuted;
        // True diameter in pixels, floored so bars stay visible when zoomed out.
        const lw = Math.max(0.6, z.diameter * view.scale);
        // Skip drawing when the spacing collapses to less than a pixel —
        // the fill already conveys the zone at that zoom.
        if (z.step * view.scale < 0.8) continue;
        ctx.strokeStyle = withAlpha(color, selected === null || selected === z.index ? 0.95 : 0.4);
        ctx.lineWidth = lw;
        ctx.beginPath();
        for (const b of z.bars) {
          ctx.moveTo(toScreenX(view, b[0]), toScreenY(view, b[1]));
          ctx.lineTo(toScreenX(view, b[2]), toScreenY(view, b[3]));
        }
        ctx.stroke();
      }
    }

    /* Zone labels ---------------------------------------------------- */
    if (showLabels) {
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      // Zones overlap heavily, so labels are placed heaviest-first and any that
      // would collide with one already drawn is dropped rather than stacked.
      const placed: [number, number, number, number][] = [];
      const collides = (r: [number, number, number, number]) =>
        placed.some((p) => r[0] < p[2] && r[2] > p[0] && r[1] < p[3] && r[3] > p[1]);

      const byMass = [...visibleZones].sort((a, b) => b.mass - a.mass);
      for (const z of byMass) {
        const [sx, sy, sw, sh] = screenRect(view, z.rect);
        if (sw < 54 || sh < 20) continue;
        const selectedHere = selected === z.index;
        const twoLine = sh > 44;
        const label = specLabel(z.diameter, z.step);
        const sub = `#${z.index + 1} · ${num(z.mass, 0)} kg`;
        ctx.font = '600 11px ui-monospace, Menlo, monospace';
        const tw = ctx.measureText(label).width;
        ctx.font = '500 10px ui-monospace, Menlo, monospace';
        const sw2 = twoLine ? ctx.measureText(sub).width : 0;
        const boxW = Math.max(tw, sw2) + 12;
        const boxH = twoLine ? 32 : 18;
        const cx = sx + sw / 2;
        const cy = sy + sh / 2;
        const box: [number, number, number, number] = [
          cx - boxW / 2 - 2,
          cy - boxH / 2 - 2,
          cx + boxW / 2 + 2,
          cy + boxH / 2 + 2,
        ];
        // The selected zone always keeps its label; others yield to it.
        if (!selectedHere && collides(box)) continue;
        placed.push(box);

        // A pill behind the text keeps it legible over the bars.
        ctx.fillStyle = withAlpha(colSurface, 0.88);
        roundRect(ctx, cx - boxW / 2, cy - boxH / 2, boxW, boxH, 4);
        ctx.fill();
        ctx.font = '600 11px ui-monospace, Menlo, monospace';
        ctx.fillStyle = colText;
        ctx.fillText(label, cx, cy + (twoLine ? -6 : 0.5));
        if (twoLine) {
          ctx.font = '500 10px ui-monospace, Menlo, monospace';
          ctx.fillStyle = colMuted;
          ctx.fillText(sub, cx, cy + 8);
        }
      }
    }

    /* Scale bar ------------------------------------------------------ */
    const barLen = niceStep(view, 110);
    const px = barLen * view.scale;
    const bx = view.width - px - 16;
    const by = view.height - 48; // clears the status pill below it
    ctx.strokeStyle = colMuted;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(bx, by - 4);
    ctx.lineTo(bx, by);
    ctx.lineTo(bx + px, by);
    ctx.lineTo(bx + px, by - 4);
    ctx.stroke();
    ctx.fillStyle = colMuted;
    ctx.font = '500 11px ui-monospace, Menlo, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText(barLen >= 1000 ? `${barLen / 1000} m` : `${barLen} mm`, bx + px / 2, by - 5);
  }, [view, visibleZones, colorScale, selected, showBars, showGrid, showLabels, showPrimary, bounds, themeKey]);

  /* ---------------------------------------------------------------- */
  /* Interaction                                                       */
  /* ---------------------------------------------------------------- */
  const hitTest = useCallback(
    (sx: number, sy: number): Zone | null => {
      if (!view) return null;
      const wx = toWorldX(view, sx);
      const wy = toWorldY(view, sy);
      let best: Zone | null = null;
      let bestArea = Infinity;
      for (const z of visibleZones) {
        const [x0, y0, x1, y1] = z.rect;
        if (wx >= x0 && wx <= x1 && wy >= y0 && wy <= y1 && z.area < bestArea) {
          best = z;
          bestArea = z.area;
        }
      }
      return best;
    },
    [view, visibleZones],
  );

  const pointer = useRef<{ id: number; x: number; y: number; moved: boolean } | null>(null);

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    pointer.current = { id: e.pointerId, x: e.clientX, y: e.clientY, moved: false };
    setPanning(true);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!view) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;

    const p = pointer.current;
    if (p && p.id === e.pointerId) {
      const dx = e.clientX - p.x;
      const dy = e.clientY - p.y;
      if (!p.moved && Math.hypot(dx, dy) < 3) return;
      p.moved = true;
      p.x = e.clientX;
      p.y = e.clientY;
      setView((v) => (v ? { ...v, cx: v.cx - dx / v.scale, cy: v.cy + dy / v.scale } : v));
      setHover(null);
      return;
    }

    setCursor({ x: toWorldX(view, sx), y: toWorldY(view, sy) });
    const z = hitTest(sx, sy);
    setHover(z ? { zone: z, sx, sy } : null);
  };

  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const p = pointer.current;
    pointer.current = null;
    setPanning(false);
    if (p && !p.moved) {
      const rect = e.currentTarget.getBoundingClientRect();
      const z = hitTest(e.clientX - rect.left, e.clientY - rect.top);
      onSelect(z ? z.index : null);
    }
  };

  // Wheel needs a non-passive listener to be able to preventDefault, which
  // React's synthetic onWheel cannot guarantee.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const factor = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015));
      setView((v) => (v ? zoomAt(v, factor, e.clientX - rect.left, e.clientY - rect.top) : v));
    };
    canvas.addEventListener('wheel', handler, { passive: false });
    return () => canvas.removeEventListener('wheel', handler);
  }, []);

  const zoomBy = (factor: number) =>
    setView((v) => (v ? zoomAt(v, factor, v.width / 2, v.height / 2) : v));

  /** Frame a single zone, without zooming in past a legible bar spacing. */
  const zoomToZone = useCallback((z: Zone) => {
    setView((v) => (v ? zoneViewport(z, v.width, v.height) : v));
  }, []);

  // Follow a selection made elsewhere (the metrics table, a summary link) into
  // view — again adjusted during render rather than from an effect.
  const [prevSelected, setPrevSelected] = useState(selected);
  if (prevSelected !== selected) {
    setPrevSelected(selected);
    const z = selected === null ? null : result.zones[selected];
    if (z) zoomToZone(z);
  }

  const toggleSpec = (key: string) =>
    setHiddenSpecs((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  /* ---------------------------------------------------------------- */
  const tooltipStyle = useMemo(() => {
    if (!hover || !view) return undefined;
    const flipX = hover.sx > view.width - 280;
    const flipY = hover.sy > view.height - 160;
    return {
      left: flipX ? undefined : hover.sx + 14,
      right: flipX ? view.width - hover.sx + 14 : undefined,
      top: flipY ? undefined : hover.sy + 14,
      bottom: flipY ? view.height - hover.sy + 14 : undefined,
    } as React.CSSProperties;
  }, [hover, view]);

  return (
    <div className="viewer" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        className={panning ? 'panning' : undefined}
        style={{ width: size.width, height: size.height }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={() => {
          setHover(null);
          setCursor(null);
        }}
        onDoubleClick={fit}
      />

      <div className="viewer-toolbar">
        <button className="sm" onClick={fit} title="Fit to extents (or double-click)">
          Fit
        </button>
        <button className="sm icon" onClick={() => zoomBy(1 / 1.3)} title="Zoom out" aria-label="Zoom out">
          −
        </button>
        <button className="sm icon" onClick={() => zoomBy(1.3)} title="Zoom in" aria-label="Zoom in">
          +
        </button>
        <span style={{ width: 1, height: 18, background: 'var(--border)' }} />
        <Toggle on={showBars} onChange={setShowBars} label="Bars" />
        <Toggle on={showLabels} onChange={setShowLabels} label="Labels" />
        <Toggle on={showPrimary} onChange={setShowPrimary} label="Primary rects" />
        <Toggle on={showGrid} onChange={setShowGrid} label="Grid" />
      </div>

      {specs.length > 0 && (
        <div className="viewer-legend">
          <div className="faint" style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.05em' }}>
            REBAR
          </div>
          {specs.map((s) => (
            <div
              key={s.specKey}
              className={`item${hiddenSpecs.has(s.specKey) ? ' off' : ''}`}
              onClick={() => toggleSpec(s.specKey)}
              role="checkbox"
              tabIndex={0}
              aria-checked={!hiddenSpecs.has(s.specKey)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  toggleSpec(s.specKey);
                }
              }}
            >
              <span className="swatch" style={{ background: colorScale.get(s.specKey) }} />
              <span className="mono">{specLabel(s.diameter, s.step)}</span>
              <span className="faint">
                {s.zones} zn · {num(s.mass, 0)} kg
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="viewer-status">
        {cursor ? `X ${num(cursor.x, 0)}  Y ${num(cursor.y, 0)} mm` : `${result.zones.length} zones`}
        {view ? ` · 1 px = ${num(1 / view.scale, 0)} mm` : ''}
      </div>

      {hover && (
        <div className="viewer-tooltip" style={tooltipStyle}>
          <div className="t">
            Zone #{hover.zone.index + 1} · {specLabel(hover.zone.diameter, hover.zone.step)}
          </div>
          <dl>
            <dt>Bars</dt>
            <dd>{hover.zone.barsCount}</dd>
            <dt>Width × length</dt>
            <dd>
              {num(hover.zone.width)} × {num(hover.zone.length)}
            </dd>
            <dt>Rebar length</dt>
            <dd>{num(hover.zone.totalBarLength / 1000, 1)} m</dd>
            <dt>Mass</dt>
            <dd>{kg(hover.zone.mass)}</dd>
            <dt>Per m²</dt>
            <dd>{num(hover.zone.massPerArea, 1)} kg/m²</dd>
          </dl>
          <div className="faint mono" style={{ marginTop: 4, fontSize: 11 }}>
            {mm(hover.zone.rect[0])}, {mm(hover.zone.rect[1])} → {mm(hover.zone.rect[2])},{' '}
            {mm(hover.zone.rect[3])}
          </div>
        </div>
      )}
    </div>
  );
}

function Toggle({
  on,
  onChange,
  label,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <button className="sm" aria-pressed={on} onClick={() => onChange(!on)} style={on ? undefined : { opacity: 0.55 }}>
      <span className="swatch" style={{ background: on ? 'var(--accent)' : 'transparent' }} />
      {label}
    </button>
  );
}

/**
 * Viewport framing one zone with generous margin, capped at 0.5 px/mm — past
 * that a small zone fills the canvas and loses all context.
 */
function zoneViewport(z: Zone, width: number, height: number): Viewport {
  const target = fitBounds(z.rect, width, height, Math.max(PAD, Math.min(width, height) * 0.22));
  return { ...target, scale: Math.min(target.scale, 0.5) };
}

function screenRect(v: Viewport, r: RectXYXY): [number, number, number, number] {
  const x = toScreenX(v, r[0]);
  const y = toScreenY(v, r[3]);
  return [x, y, toScreenX(v, r[2]) - x, toScreenY(v, r[1]) - y];
}

function strokeRect(ctx: CanvasRenderingContext2D, v: Viewport, r: RectXYXY) {
  const [x, y, w, h] = screenRect(v, r);
  ctx.strokeRect(x, y, w, h);
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
