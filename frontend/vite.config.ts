import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import pkg from './package.json' with { type: 'json' }

// The backend serves both the JSON API (/api/*) and the Plex OAuth callback
// (/auth/callback) from the same origin in production, so dev proxies both
// prefixes to the local uvicorn instance. Session cookies are `secure` +
// same-site lax, which the proxy preserves because the browser only ever
// talks to the Vite origin.
const BACKEND = 'http://localhost:8000'

// The build-time name. Only a fallback — the running instance's name comes
// from GET /api/config (src/lib/branding.svelte.ts) — but it is the fallback
// that a cold load actually paints: the tab title before any script runs, and
// the splash's first frame.
const APP_NAME = process.env.APP_NAME || 'Quorarr'

export default defineConfig({
  plugins: [
    svelte(),
    {
      // index.html is static, so Vite's own %VITE_*% substitution is the only
      // built-in path and it requires a VITE_-prefixed variable. One explicit
      // hook is clearer than a naming convention nobody would guess.
      name: 'app-name-in-html',
      transformIndexHtml: (html: string) => html.replaceAll('%APP_NAME%', APP_NAME),
    },
  ],
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
    // Build-time fallback for the splash frame before /api/config answers.
    // Not the source of truth — see src/lib/branding.svelte.ts.
    __APP_NAME__: JSON.stringify(APP_NAME),
  },
  server: {
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/auth': { target: BACKEND, changeOrigin: true },
    },
  },
})
