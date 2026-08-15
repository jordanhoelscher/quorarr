/**
 * Service worker.
 *
 * Two rules matter here:
 *   1. NEVER cache /api/ or /auth/. The app's whole job is reporting live
 *      server state; a cached storage figure or approval queue is worse than
 *      no answer. Staleness is handled server-side (CachedHTTP), not here.
 *   2. Built assets are content-hashed by Vite, so they are safe to serve
 *      cache-first forever. The shell document is not hashed, so it is served
 *      stale-while-revalidate: instant offline start, and a deploy lands on
 *      the next load instead of pinning users to an old bundle.
 *
 * Bump CACHE when the caching strategy changes; app releases do not need a
 * bump because hashed asset URLs and the revalidated shell handle themselves.
 *
 * Since 0.3.0 this worker is also the push receiver. The two push handlers are
 * deliberately independent of everything above: they never read or write the
 * cache, and a change to the caching strategy must not disturb them.
 */

// A static file cannot read APP_NAME, and this name is never shown to
// anyone -- it is a cache key. Keep it brand-free rather than templated.
const CACHE = 'app-shell-v1';

/** Fetched at install so a cold offline launch still boots. */
const SHELL = ['/', '/manifest.json', '/icons/icon-192.png', '/icons/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

/** Cache a successful, basic (same-origin, non-opaque) response. */
const put = (request, response) => {
  if (response && response.ok && response.type === 'basic') {
    const clone = response.clone();
    void caches.open(CACHE).then((cache) => cache.put(request, clone));
  }
  return response;
};

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Network only, no interception at all: API and the Plex OAuth callback.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/')) return;

  // Never touch writes, cross-origin requests (fonts), or non-GET verbs.
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  // Shell document: serve cached immediately, refresh in the background.
  if (request.mode === 'navigate') {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match('/');
        const network = fetch(request)
          .then((response) => put('/', response))
          .catch(() => cached ?? Response.error());
        return cached || network;
      }),
    );
    return;
  }

  // Hashed assets and static files: cache-first, populate on miss.
  event.respondWith(
    caches.match(request).then(
      (cached) =>
        cached ||
        fetch(request)
          .then((response) => put(request, response))
          .catch(() => Response.error()),
    ),
  );
});


/* ------------------------------------------------------------------- push */

/**
 * Payload contract with the backend's `push.py`:
 *   { title: string, body: string, tab: 'approvals' | 'flagged' | 'pipeline' }
 *
 * `userVisibleOnly: true` is a promise to the browser that every push shows a
 * notification, so an unreadable or empty payload still surfaces something
 * generic rather than being silently dropped (which costs the subscription).
 */
self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = {};
  }

  // The backend always sends a title; this is the corrupt-payload floor.
  const title = payload.title || 'Notification';
  event.waitUntil(
    self.registration.showNotification(title, {
      body: payload.body || '',
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      // Same tag per tab: a second nudge about the same queue replaces the
      // first instead of stacking a lock screen full of near-identical rows.
      tag: `notify-${payload.tab || 'general'}`,
      data: { tab: payload.tab || null },
    }),
  );
});

/**
 * Tapping a notification focuses an open app window if there is one,
 * otherwise opens a new one — either way landing on the tab the notification
 * is about, passed as `?tab=`.
 */
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const tab = event.notification.data && event.notification.data.tab;
  const url = tab ? `/?tab=${encodeURIComponent(tab)}` : '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if (new URL(client.url).origin !== self.location.origin) continue;
        // navigate() can reject (cross-origin, or an unsupported client) —
        // focusing the existing window still beats opening a second one.
        return client.focus().then((focused) => (focused || client).navigate(url).catch(() => {}));
      }
      return self.clients.openWindow(url);
    }),
  );
});
