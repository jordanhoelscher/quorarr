/**
 * Who this instance says it is.
 *
 * One codebase, many deployments: the product name, the person approvals go
 * to, and the server's own name all come from the backend at runtime rather
 * than being baked into the bundle. That is what lets a published image be
 * rebranded with an environment variable instead of a rebuild.
 *
 * The build-time `__APP_NAME__` is the pre-config fallback, not a second
 * source of truth — it paints the splash for the one round trip before
 * `/api/config` answers, and it is what a stranger sees if the backend is
 * unreachable (in which case the name is the least of their problems).
 */

import { api } from './api';

interface AppConfig {
  app_name: string;
  owner_name: string;
  server_name: string;
  client_id: string;
  version: string;
}

export const branding = $state({
  appName: __APP_NAME__,
  /** Reads correctly in every sentence that uses it, name or not. */
  ownerName: 'the owner',
  /** Subtitle under the wordmark; empty means "don't show a subtitle". */
  serverName: '',
  /**
   * Whether `loadBranding` has finished — either way.
   *
   * The build-time name is a *fallback*, not a preview: on an instance that
   * renamed itself, painting it would flash the wrong name for a frame and
   * then swap. So the splash holds its wordmark until this flips, and flips
   * it on failure too, or a backend that never answers would leave the app
   * nameless forever.
   */
  resolved: false,
});

/**
 * The one error line for "the backend did not answer".
 *
 * Thirteen views had their own copy of this sentence in three slightly
 * different wordings. One function means a rebrand — or a rewording — is one
 * edit, and it is evaluated at throw time so it always names the instance
 * config actually reported.
 */
export const unreachable = (): string =>
  `Could not reach ${branding.appName}. Try again in a moment.`;

/** Said in two places when a friend's ask has been parked for the owner. */
export const SENT_FOR_APPROVAL = (): string => `Sent to ${branding.ownerName} for approval`;

/**
 * Fetch this instance's identity and apply it to the document.
 *
 * Failure is deliberately silent: the app runs on the build-time fallback,
 * and whatever broke the config fetch is about to surface as a much louder
 * error on the session check that follows.
 */
export const loadBranding = async (): Promise<void> => {
  try {
    const config = await api.get<AppConfig>('/api/config');
    branding.appName = config.app_name;
    branding.ownerName = config.owner_name;
    branding.serverName = config.server_name;
  } catch {
    return;
  } finally {
    branding.resolved = true;
  }

  if (typeof document === 'undefined') return;
  document.title = branding.appName;
  // iOS reads this meta tag — not the manifest — when naming a Home Screen
  // icon added from Safari, and it reads it from the live DOM at the moment
  // the user adds it. So updating it here genuinely renames the icon, unlike
  // the static value in index.html which is only ever the fallback.
  const appleTitle = document.querySelector('meta[name="apple-mobile-web-app-title"]');
  if (appleTitle) appleTitle.setAttribute('content', branding.appName);
};
