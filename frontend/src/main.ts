import { mount } from 'svelte';

import './app.css';
import App from './App.svelte';

const app = mount(App, { target: document.getElementById('app')! });

// Dev has no built asset URLs to cache and a stale SW would shadow HMR, so the
// worker is production-only. Registration failure is non-fatal — the app runs
// fine online without it.
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

export default app;
