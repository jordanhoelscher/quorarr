/**
 * Starting a Plex sign-in — from the friend's own browser where possible.
 *
 * The PIN handshake works either way, but *who mints the PIN* decides what
 * Plex tells the friend afterwards. When the backend mints it, plex.tv sees
 * the request come from the server's datacentre address, and Plex emails a
 * "Security Alert — a new device signed in" naming a city the friend has
 * never been to. Nothing is wrong; it just reads exactly like a breach, and
 * it is the first thing a new friend sees.
 *
 * So the browser mints its own PIN against plex.tv directly — CORS is open
 * there (`access-control-allow-origin: *`, verified live 2026-08-13) — and
 * hands the id and code to the backend, which signs them into the short-lived
 * PIN cookie and builds the hosted auth URL. If any of that fails, the old
 * server-minted path is still there: a scary-looking email beats no sign-in.
 */

import { api } from './api';

const PLEX_PINS_URL = 'https://plex.tv/api/v2/pins?strong=true';

interface LoginStart {
  auth_url: string;
}

/** The public identity payload the SPA boots from (`GET /api/config`). */
interface AppConfig {
  app_name: string;
  client_id: string;
}

/** The half of the handshake plex.tv hands back to whoever minted the PIN. */
export interface BrowserPin {
  pin_id: number;
  code: string;
}

/**
 * Mint a PIN at plex.tv from this browser.
 *
 * @param clientId The `X-Plex-Client-Identifier` the backend will poll with.
 *   It has to match, or plex.tv answers 404 on the poll.
 * @param product The `X-Plex-Product`, from `/api/config`. This is the name
 *   that shows up in the user's Plex *Authorized Devices* list, and the
 *   backend sends the same value on its own calls — passing it in (rather
 *   than hardcoding it here) is what keeps the two sides from drifting into
 *   two differently named entries.
 * @throws If plex.tv is unreachable, refuses, or answers something that
 *   isn't a PIN — every one of which is a cue to fall back.
 */
export const mintPin = async (clientId: string, product: string): Promise<BrowserPin> => {
  const response = await fetch(PLEX_PINS_URL, {
    method: 'POST',
    headers: {
      'X-Plex-Product': product,
      'X-Plex-Client-Identifier': clientId,
      Accept: 'application/json',
    },
  });
  if (!response.ok) throw new Error(`plex.tv answered ${response.status}`);

  const body: unknown = await response.json();
  const { id, code } = (body ?? {}) as { id?: unknown; code?: unknown };
  if (typeof id !== 'number' || typeof code !== 'string' || !code) {
    throw new Error('plex.tv returned no PIN');
  }
  return { pin_id: id, code };
};

/**
 * The URL to send the browser to for sign-in.
 *
 * Tries the browser-minted path, falls back to the server-minted one on any
 * failure — an extension blocking plex.tv, a captive portal, a future CORS
 * change. The fallback is deliberately blind to *which* step failed: the
 * only thing that matters here is that the friend can still sign in, and the
 * server path re-does the whole handshake from scratch.
 */
export const startLogin = async (): Promise<string> => {
  try {
    const { app_name, client_id } = await api.get<AppConfig>('/api/config');
    const pin = await mintPin(client_id, app_name);
    const { auth_url } = await api.post<LoginStart>('/api/auth/login', pin);
    return auth_url;
  } catch {
    const { auth_url } = await api.get<LoginStart>('/api/auth/login');
    return auth_url;
  }
};
