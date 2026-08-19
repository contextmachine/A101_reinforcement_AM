/** Minimal RFC-4180 CSV writer plus a browser download helper. */

const escapeCell = (value: unknown): string => {
  const s = value === null || value === undefined ? '' : String(value);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

export function toCsv(rows: unknown[][]): string {
  return rows.map((row) => row.map(escapeCell).join(',')).join('\r\n');
}

export function downloadBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  click(url, fileName);
  // Revoking synchronously can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/**
 * Drive a download from a synthetic link.
 *
 * The link is detached on the next tick, not synchronously: Chromium aborts a
 * download whose initiating element leaves the document in the same task.
 */
function click(href: string, fileName?: string) {
  const a = document.createElement('a');
  a.href = href;
  a.rel = 'noopener';
  if (fileName) a.download = fileName;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 0);
}

export function downloadCsv(rows: unknown[][], fileName: string) {
  // BOM so Excel picks up UTF-8 for the ⌀ and ² glyphs.
  downloadBlob(new Blob(['﻿' + toCsv(rows)], { type: 'text/csv;charset=utf-8' }), fileName);
}

export function downloadText(text: string, fileName: string, type = 'application/json') {
  downloadBlob(new Blob([text], { type: `${type};charset=utf-8` }), fileName);
}

/**
 * Follow a URL that responds with `Content-Disposition: attachment`.
 *
 * A synthetic link keeps the SPA mounted; assigning to `location.href` would
 * count as a navigation and tear the app down if the server ever answered with
 * something renderable instead of an attachment.
 */
export function openDownload(href: string) {
  click(href);
}
