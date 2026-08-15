import { ApiError, api, setUnauthorizedHandler } from './api';
import { unreachable } from './branding.svelte';

export type Role = 'owner' | 'member';

export interface User {
  id: number;
  name: string;
  role: Role;
}

/**
 * Who is signed in, as reactive runes state.
 *
 * `loading` is true only for the first `load()` — the app shows a splash
 * while the cookie is being validated, and must not flash the login screen at
 * a user who is in fact signed in.
 */
class Session {
  user = $state<User | null>(null);
  loading = $state(true);
  loggedOut = $state(false);
  /** Non-null when `load()` failed for a reason other than "not signed in". */
  error = $state<string | null>(null);

  /** Fetch the current user. A 401 is the normal signed-out answer, not an error. */
  async load(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      this.user = await api.get<User>('/api/auth/me');
      this.loggedOut = false;
    } catch (err) {
      this.user = null;
      // 401 already flipped `loggedOut` through the unauthorized handler.
      if (!(err instanceof ApiError && err.status === 401)) {
        this.error = err instanceof Error ? err.message : unreachable();
      }
    } finally {
      this.loading = false;
    }
  }

  /**
   * Clear the session cookie and drop back to the login screen.
   *
   * A failing logout still clears local state: the user asked to be signed
   * out, and leaving them in a half-authenticated UI is worse than a stale
   * cookie the next `load()` will resolve anyway.
   */
  async logout(): Promise<void> {
    try {
      await api.post<void>('/api/auth/logout');
    } finally {
      this.user = null;
      this.loggedOut = true;
    }
  }
}

export const session = new Session();

// Any 401 anywhere in the app means the cookie died — drop to the login screen.
setUnauthorizedHandler(() => {
  session.user = null;
  session.loggedOut = true;
});
