import { afterEach, describe, expect, it, vi } from 'vitest';

import { mintPin, startLogin } from './plexLogin';

const PIN = { id: 1917492240, code: 'zufn6nlbf1ixfoeu8ebj07rps' };

/** Stand in for the browser's fetch, routing by URL. */
const mockFetch = (handler: (url: string, init?: RequestInit) => Response): void => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      Promise.resolve(handler(String(input), init)),
    ),
  );
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('mintPin', () => {
  it('sends the headers plex.tv requires and returns the pin', async () => {
    let seen: RequestInit | undefined;
    mockFetch((url, init) => {
      expect(url).toBe('https://plex.tv/api/v2/pins?strong=true');
      seen = init;
      return json(PIN, 201);
    });

    expect(await mintPin('quorarr-abc', 'Quorarr')).toEqual({
      pin_id: PIN.id,
      code: PIN.code,
    });
    expect(seen?.method).toBe('POST');
    const headers = seen?.headers as Record<string, string>;
    // Not a literal: this is the name in the user's Plex Authorized Devices
    // list, and it comes from APP_NAME via /api/config so both sides agree.
    expect(headers['X-Plex-Product']).toBe('Quorarr');
    // Must match what the backend polls with, or plex.tv 404s the poll.
    expect(headers['X-Plex-Client-Identifier']).toBe('quorarr-abc');
    expect(headers.Accept).toBe('application/json');
  });

  it('throws on a refusal rather than returning a half-pin', async () => {
    mockFetch(() => json({ errors: [] }, 429));
    await expect(mintPin('x', 'Quorarr')).rejects.toThrow();
  });

  it('throws when plex.tv answers something that is not a pin', async () => {
    mockFetch(() => json({ id: 'not-a-number', code: 'abc' }, 201));
    await expect(mintPin('x', 'Quorarr')).rejects.toThrow();
  });
});

describe('startLogin', () => {
  it('mints in the browser and hands the pin to the backend', async () => {
    const posted: unknown[] = [];
    mockFetch((url, init) => {
      if (url === '/api/config') return json({ app_name: 'Quorarr', client_id: 'live-id' });
      if (url === 'https://plex.tv/api/v2/pins?strong=true') return json(PIN, 201);
      if (url === '/api/auth/login') {
        posted.push(JSON.parse(String(init?.body)));
        return json({ auth_url: 'https://app.plex.tv/auth#?code=' + PIN.code });
      }
      throw new Error(`unexpected ${url}`);
    });

    expect(await startLogin()).toContain(PIN.code);
    expect(posted).toEqual([{ pin_id: PIN.id, code: PIN.code }]);
  });

  it('falls back to the server-minted flow when plex.tv is unreachable', async () => {
    // The CORS case: the browser's cross-origin call simply rejects.
    const calls: string[] = [];
    mockFetch((url, init) => {
      calls.push(`${init?.method ?? 'GET'} ${url}`);
      if (url === '/api/config') return json({ app_name: 'Quorarr', client_id: 'live-id' });
      if (url.startsWith('https://plex.tv')) throw new TypeError('Failed to fetch');
      if (url === '/api/auth/login') return json({ auth_url: 'https://app.plex.tv/server' });
      throw new Error(`unexpected ${url}`);
    });

    expect(await startLogin()).toBe('https://app.plex.tv/server');
    expect(calls).toContain('GET /api/auth/login');
    expect(calls).not.toContain('POST /api/auth/login');
  });

  it('falls back when the backend cannot say which client id to use', async () => {
    mockFetch((url) => {
      if (url === '/api/config') return json({ error: 'nope' }, 500);
      if (url === '/api/auth/login') return json({ auth_url: 'https://app.plex.tv/server' });
      throw new Error(`unexpected ${url}`);
    });

    expect(await startLogin()).toBe('https://app.plex.tv/server');
  });

  it('surfaces the failure when the fallback fails too', async () => {
    mockFetch(() => json({ error: 'down' }, 502));
    await expect(startLogin()).rejects.toThrow();
  });
});
