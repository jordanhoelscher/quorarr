/**
 * One reading of "can this browser be reached", shared by everything that
 * shows it.
 *
 * The bell used to own this privately, which was fine while it was the only
 * notification control. It isn't: the onboarding card (v0.4.1) offers the same
 * subscription, and a card that enables push while the bell still reads "off"
 * is the app contradicting itself on screen. So the five outcomes live here,
 * detected once, and both surfaces render the same value.
 */
import { getSubscription, pushSupported } from './push';

/**
 * - **unknown** — not yet detected; render nothing.
 * - **unsupported** — no push in this browser at all.
 * - **install** — iOS, not installed to the Home Screen. `pushManager.subscribe`
 *   refuses outside a Home Screen app and the failure is a bare exception, so
 *   this is a distinct state with its own instruction rather than an error.
 * - **denied** — permission refused for this origin; the browser will not
 *   re-prompt, so no control can help.
 * - **off / on** — subscribable, and subscribed.
 */
export type PushMode = 'unknown' | 'unsupported' | 'install' | 'denied' | 'off' | 'on';

/** iPadOS 13+ reports itself as Macintosh; touch points are the tell. */
const isIos = (): boolean =>
  typeof navigator !== 'undefined' &&
  (/iP(hone|od|ad)/.test(navigator.userAgent) ||
    (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1));

const isStandalone = (): boolean =>
  typeof window !== 'undefined' &&
  (window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true);

class PushState {
  mode = $state<PushMode>('unknown');
  /**
   * The browser's own permission answer, or null where there is no push API.
   * Kept separate from `mode` because "granted but not subscribed" and "never
   * asked" are both `off`, and only the second is worth interrupting someone
   * with an onboarding card over.
   */
  permission = $state<NotificationPermission | null>(null);

  private probe: Promise<void> | null = null;

  /** Detect once, however many surfaces ask. Safe to call from every mount. */
  detect(): Promise<void> {
    this.probe ??= this.read();
    return this.probe;
  }

  private async read(): Promise<void> {
    if (isIos() && !isStandalone()) {
      this.mode = 'install';
      return;
    }
    if (!pushSupported()) {
      this.mode = 'unsupported';
      return;
    }

    this.permission = Notification.permission;
    if (this.permission === 'denied') {
      this.mode = 'denied';
      return;
    }
    this.mode = (await getSubscription()) ? 'on' : 'off';
  }

  /**
   * Record how an enable attempt actually ended.
   *
   * Re-reads `Notification.permission` rather than trusting the boolean: a
   * refusal that flipped the browser to `denied` has to stop offering a button
   * that can no longer raise a prompt.
   */
  settle(granted: boolean): void {
    this.permission = pushSupported() ? Notification.permission : null;
    this.mode = granted ? 'on' : this.permission === 'denied' ? 'denied' : 'off';
  }
}

export const pushState = new PushState();
