/** Display formatters. Pure functions — no DOM, no state. */

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const;
const MS_PER_DAY = 86_400_000;

/**
 * Format a byte count as a one-decimal human string ("1.5 TB").
 *
 * Uses 1024-based steps and stops at TB, so anything petabyte-scale keeps
 * counting in TB rather than inventing a unit nobody eyeballs. Zero, negative,
 * and non-finite inputs all collapse to "0 B" — upstream size fields are
 * nullable and a missing size is not a negative library.
 */
export const formatBytes = (n: number): string => {
  if (!Number.isFinite(n) || n <= 0) return '0 B';

  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }

  // Bytes are whole things; everything above gets one decimal.
  const shown = unit === 0 ? Math.round(value).toString() : value.toFixed(1);
  return `${shown} ${BYTE_UNITS[unit]}`;
};

/**
 * Format an ISO timestamp as a coarse relative age ("3h ago").
 *
 * Deliberately coarse: the app shows when something was requested or flagged,
 * where "2d ago" is the useful precision and a live-ticking clock is noise.
 * Future timestamps read as "just now" rather than negative.
 */
export const timeAgo = (iso: string): string => {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return 'unknown';

  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return 'just now';

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;

  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;

  return `${Math.floor(days / 365)}y ago`;
};

/**
 * Label for a stale-cache response's `stale_seconds` ("as of 3 min ago").
 *
 * Deliberately reads as an aside rather than an alarm: the data is real, just
 * not fresh, and the backend only ever serves it when the upstream is down.
 */
export const staleLabel = (seconds: number): string => {
  if (!Number.isFinite(seconds) || seconds < 60) return 'as of just now';

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `as of ${minutes} min ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `as of ${hours}h ago`;

  return `as of ${Math.floor(hours / 24)}d ago`;
};

/**
 * Format a Radarr/Sonarr `timeleft` as a short duration ("1h 23m").
 *
 * Accepts both the plain `"HH:MM:SS"` form and the day-prefixed
 * `"D.HH:MM:SS"` form the arrs emit past 24 hours. Returns null for anything
 * unparseable, so callers can omit the field rather than print `"NaNh"`.
 */
export const formatTimeleft = (raw: string | null | undefined): string | null => {
  if (!raw) return null;

  let days = 0;
  let rest = raw;
  const dot = raw.indexOf('.');
  if (dot !== -1) {
    days = Number(raw.slice(0, dot));
    rest = raw.slice(dot + 1);
  }

  const parts = rest.split(':');
  if (parts.length !== 3) return null;

  const [hours, minutes, seconds] = parts.map(Number);
  if (![days, hours, minutes, seconds].every(Number.isInteger)) return null;

  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${seconds}s`;
};

/**
 * Whole days remaining in a deletion window that opened at `flaggedAtIso`.
 *
 * Clamped at 0 — an elapsed window is "0 days left", never a negative
 * countdown, because the backend sweep may not have moved the row to
 * pending_approval yet when the client renders.
 */
export const daysLeft = (flaggedAtIso: string, windowDays: number): number => {
  const flaggedAt = Date.parse(flaggedAtIso);
  if (Number.isNaN(flaggedAt)) return 0;

  const elapsedDays = (Date.now() - flaggedAt) / MS_PER_DAY;
  return Math.max(0, Math.ceil(windowDays - elapsedDays));
};

/** Where Jellyseerr's poster paths actually live. */
const TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p';

/**
 * Absolute TMDB poster URL from the relative path Jellyseerr hands out.
 *
 * Jellyseerr returns `"/abc123.jpg"` and expects the client to pick a size —
 * unlike Radarr/Sonarr, which give the library views a fully-formed URL. `w342`
 * is the right rung for a phone-width poster tile: sharp on a 2x screen without
 * pulling an original-resolution still into a scroll rail.
 *
 * Null in, null out, so a title with no artwork falls through to `Poster`'s
 * carved-stone placeholder rather than requesting `.../w342/null`.
 */
export const posterUrl = (path: string | null | undefined, size = 'w342'): string | null =>
  path ? `${TMDB_IMAGE_BASE}/${size}${path.startsWith('/') ? '' : '/'}${path}` : null;
