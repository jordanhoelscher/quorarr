/// <reference types="svelte" />
/// <reference types="vite/client" />

/** Injected by Vite `define` from package.json `version`. */
declare const __APP_VERSION__: string;

/**
 * Injected by Vite `define` from `$APP_NAME` at build time.
 *
 * The pre-config fallback only: the running instance's name comes from
 * `/api/config` (see `lib/branding.svelte.ts`).
 */
declare const __APP_NAME__: string;
