/**
 * Thin fetch wrapper for the backend.
 *
 * The backend is same-origin in production (FastAPI mounts the built SPA) and
 * proxied same-origin in dev, so auth is the `pensieve_session` cookie and
 * nothing here ever handles a token.
 */

/** A non-2xx response, carrying the status and the server's message. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

type UnauthorizedHandler = () => void;

let onUnauthorized: UnauthorizedHandler | null = null;

/**
 * Register the callback fired on any 401.
 *
 * The session store registers itself here at import time. Inverting it this
 * way (rather than importing the store) keeps the dependency one-directional —
 * `session` depends on `api`, never the reverse.
 */
export const setUnauthorizedHandler = (handler: UnauthorizedHandler): void => {
  onUnauthorized = handler;
};

/**
 * Best-effort human message from an error response body.
 *
 * The app's own routes answer `{"error": "..."}`; FastAPI's own guards
 * (401, 429, validation) answer `{"detail": "..."}`. Anything else — an HTML
 * error page from a proxy, an empty body — falls back to the status line, so
 * a caller always has something to show.
 */
const errorMessage = async (response: Response): Promise<string> => {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === 'object') {
      const { error, detail } = body as { error?: unknown; detail?: unknown };
      if (typeof error === 'string' && error) return error;
      if (typeof detail === 'string' && detail) return detail;
    }
  } catch {
    // Non-JSON or empty body — fall through to the generic message.
  }
  return response.statusText || `Request failed (${response.status})`;
};

const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, { credentials: 'same-origin', ...init });

  if (response.status === 401) onUnauthorized?.();

  if (!response.ok) throw new ApiError(response.status, await errorMessage(response));

  // 204 (logout) and other empty bodies have nothing to parse.
  if (response.status === 204 || response.headers.get('content-length') === '0') {
    return undefined as T;
  }
  return (await response.json()) as T;
};

export const api = {
  get: <T>(path: string): Promise<T> => request<T>(path),

  post: <T>(path: string, body?: unknown): Promise<T> =>
    request<T>(
      path,
      body === undefined
        ? { method: 'POST' }
        : {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          },
    ),
};
