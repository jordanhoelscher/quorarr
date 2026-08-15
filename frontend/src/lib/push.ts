/**
 * Web Push subscription plumbing.
 *
 * The browser owns the subscription; the backend only owns a copy of where to
 * reach it. Both have to be told, and they can drift — a browser can drop a
 * subscription on its own (permission reset, storage clear), and the backend
 * prunes an endpoint the moment a push comes back 404/410. `getSubscription`
 * therefore reads the *browser*, which is the side that can say no.
 *
 * Every entry point is safe to call on a browser with no push support at all:
 * `pushSupported()` gates the UI, and each function re-checks rather than
 * trusting the caller.
 */
import { api } from './api';

/** Whether this browser can register for push at all. */
export const pushSupported = (): boolean =>
  typeof navigator !== 'undefined' &&
  typeof window !== 'undefined' &&
  'serviceWorker' in navigator &&
  'PushManager' in window &&
  'Notification' in window;

/**
 * Decode the backend's base64url VAPID key into the `Uint8Array` that
 * `PushManager.subscribe` demands.
 *
 * Standard base64url: `-`/`_` for `+`/`/`, and the `=` padding stripped —
 * `atob` accepts neither, so both have to be put back first.
 */
export const urlBase64ToUint8Array = (base64url: string): Uint8Array<ArrayBuffer> => {
  const padding = (4 - (base64url.length % 4)) % 4;
  const base64 = (base64url + '='.repeat(padding)).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
};

/** Whether two application server keys are the same bytes. */
const sameKey = (a: ArrayBuffer | null, b: Uint8Array<ArrayBuffer>): boolean => {
  if (!a) return false;
  const view = new Uint8Array(a);
  return view.length === b.length && view.every((byte, i) => byte === b[i]);
};

/** This browser's current push subscription, or null. Never throws. */
export const getSubscription = async (): Promise<PushSubscription | null> => {
  if (!pushSupported()) return null;
  try {
    const registration = await navigator.serviceWorker.getRegistration();
    return (await registration?.pushManager.getSubscription()) ?? null;
  } catch {
    return null;
  }
};

/**
 * Ask for permission, subscribe, and register the endpoint with the backend.
 *
 * **Must be called straight from a click handler** — iOS only honours
 * `Notification.requestPermission()` inside a user gesture, and an awaited
 * network call before it would break that chain.
 *
 * Returns true only when the backend has the subscription. A refused
 * permission is a false, not an error: the user answered the question.
 *
 * @throws Whatever `api` throws if the backend calls fail, so the caller can
 *   surface a real message rather than a silent no-op.
 */
export const enablePush = async (): Promise<boolean> => {
  if (!pushSupported()) return false;

  const permission = await Notification.requestPermission();
  if (permission !== 'granted') return false;

  const { key } = await api.get<{ key: string }>('/api/push/public-key');
  if (!key) return false;

  const applicationServerKey = urlBase64ToUint8Array(key);
  const registration = await navigator.serviceWorker.ready;

  // An existing subscription bound to a *previous* VAPID key would be
  // accepted here and then rejected by the push service on every send, which
  // looks exactly like "notifications are broken". Rotating the keypair has
  // to invalidate it, so compare before reusing.
  let subscription = await registration.pushManager.getSubscription();
  if (subscription && !sameKey(subscription.options.applicationServerKey, applicationServerKey)) {
    await subscription.unsubscribe();
    subscription = null;
  }
  subscription ??= await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey,
  });

  const { keys } = subscription.toJSON();
  await api.post('/api/push/subscribe', {
    subscription: { endpoint: subscription.endpoint, keys },
  });
  return true;
};

/**
 * Stop notifications: forget the endpoint server-side, then drop it locally.
 *
 * Backend first, because that is the side that actually sends. If the browser
 * half then fails, nothing is delivered anyway; if the *backend* half fails
 * and the browser one succeeds, the next push comes back 410 and the server
 * prunes the row itself — so neither order can strand a live subscription.
 */
export const disablePush = async (): Promise<void> => {
  const subscription = await getSubscription();
  if (!subscription) return;

  await api.post('/api/push/unsubscribe', { endpoint: subscription.endpoint });
  await subscription.unsubscribe();
};

/** Marks that this device has already been offered notifications once. */
export const ONBOARDED_KEY = 'pensieve_onboarded';

/**
 * Whether the first-login notification card has already had its turn here.
 *
 * Per device, not per account: the thing being offered is a *browser*
 * subscription, so the same person on a phone and a laptop is two separate
 * invitations. Storage failures (Safari private mode throws on write, and
 * `localStorage` simply does not exist under SSR or in the test runner) read
 * as "not yet onboarded" — the worst case is showing a dismissible card one
 * extra time, whereas throwing would take the whole shell down with it.
 */
export const isOnboarded = (): boolean => {
  try {
    return localStorage.getItem(ONBOARDED_KEY) !== null;
  } catch {
    return false;
  }
};

/** Remember that the offer was made, however it was answered. Never throws. */
export const markOnboarded = (): void => {
  try {
    localStorage.setItem(ONBOARDED_KEY, new Date().toISOString());
  } catch {
    // Storage is unavailable or full; the card reappearing once is survivable.
  }
};
