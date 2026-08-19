import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * The solver backend does not exist yet. When it does, set VITE_PROXY_TARGET to
 * it and `/api` is forwarded there in development, so the browser keeps talking
 * to a single origin. Without it no proxy is registered and the app runs in its
 * open-a-result-file mode.
 */
const target = process.env.VITE_PROXY_TARGET;

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    ...(target ? { proxy: { '/api': { target, changeOrigin: true } } } : {}),
  },
  build: { outDir: 'dist', sourcemap: true },
});
