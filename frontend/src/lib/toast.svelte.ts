/**
 * Transient messages for actions that leave the screen behind them.
 *
 * The app's writes (flag, veto, quality request) all close the surface that
 * triggered them, so the result has nowhere to live inline — a toast is the
 * only place the server's answer can be shown. Two tones only: `info` for a
 * request that landed, `warn` for one the server refused (409 cooldown,
 * duplicate, upstream down).
 */

export type ToastTone = 'info' | 'warn';

export interface Toast {
  id: number;
  message: string;
  tone: ToastTone;
}

/** How long a toast stays up. Long enough to read a cooldown message. */
const LIFETIME_MS = 5200;

let nextId = 0;

class Toasts {
  items = $state<Toast[]>([]);

  /** Show a message. Returns nothing — callers never need to track a toast. */
  push(message: string, tone: ToastTone = 'info'): void {
    const id = (nextId += 1);
    this.items = [...this.items, { id, message, tone }];
    setTimeout(() => this.dismiss(id), LIFETIME_MS);
  }

  /** Remove a toast early. Idempotent — the lifetime timer still fires later. */
  dismiss(id: number): void {
    this.items = this.items.filter((toast) => toast.id !== id);
  }
}

export const toasts = new Toasts();
