import { useCallback, useEffect, useState } from 'react';
import { invalidateColorCache } from './colors';

export type Theme = 'light' | 'dark';

const KEY = 'a101.theme';

function initial(): Theme {
  const stored = localStorage.getItem(KEY);
  if (stored === 'light' || stored === 'dark') return stored;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/**
 * Theme state, stamped onto `<html data-theme>`. Canvas and SVG read their
 * colours from CSS variables, so the cache in colors.ts has to be dropped on
 * every switch; `key` is bumped so consumers can re-render.
 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initial);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(KEY, theme);
    invalidateColorCache();
  }, [theme]);

  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), []);

  return { theme, toggle };
}
