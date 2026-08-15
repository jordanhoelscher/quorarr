import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { daysLeft, formatBytes, formatTimeleft, posterUrl, staleLabel, timeAgo } from './format';

/** Fixed "now" so relative formatting is deterministic. */
const NOW = new Date('2026-08-13T12:00:00.000Z');
const MS_PER_DAY = 86_400_000;

/** ISO string for a moment `ms` milliseconds before NOW. */
const agoIso = (ms: number): string => new Date(NOW.getTime() - ms).toISOString();

describe('formatBytes', () => {
  it('collapses zero, negative, and non-finite sizes to "0 B"', () => {
    // Upstream size fields are nullable; a missing size is not a negative library.
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(-1)).toBe('0 B');
    expect(formatBytes(-(1024 ** 4))).toBe('0 B');
    expect(formatBytes(Number.NaN)).toBe('0 B');
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe('0 B');
  });

  it('shows raw bytes with no decimal below 1 KB', () => {
    expect(formatBytes(1)).toBe('1 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1023)).toBe('1023 B');
  });

  it('steps through KB, MB, GB, TB at 1024 boundaries', () => {
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(1024 ** 2)).toBe('1.0 MB');
    expect(formatBytes(1024 ** 3)).toBe('1.0 GB');
    expect(formatBytes(1024 ** 4)).toBe('1.0 TB');
  });

  it('keeps exactly one decimal', () => {
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(2.5 * 1024 ** 3)).toBe('2.5 GB');
    expect(formatBytes(1.5 * 1024 ** 4)).toBe('1.5 TB');
  });

  it('rounds to the nearest tenth rather than truncating', () => {
    expect(formatBytes(1024 * 1.26)).toBe('1.3 KB');
    expect(formatBytes(1024 * 1.24)).toBe('1.2 KB');
  });

  it('stops at TB instead of inventing a larger unit', () => {
    expect(formatBytes(1024 ** 5)).toBe('1024.0 TB');
    expect(formatBytes(12 * 1024 ** 4)).toBe('12.0 TB');
  });
});

describe('timeAgo', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('reads sub-minute and future timestamps as "just now"', () => {
    expect(timeAgo(agoIso(0))).toBe('just now');
    expect(timeAgo(agoIso(30_000))).toBe('just now');
    expect(timeAgo(agoIso(-60_000))).toBe('just now');
  });

  it('counts minutes below an hour', () => {
    expect(timeAgo(agoIso(60_000))).toBe('1m ago');
    expect(timeAgo(agoIso(45 * 60_000))).toBe('45m ago');
    expect(timeAgo(agoIso(59 * 60_000))).toBe('59m ago');
  });

  it('counts hours below a day', () => {
    expect(timeAgo(agoIso(3 * 3_600_000))).toBe('3h ago');
    expect(timeAgo(agoIso(23 * 3_600_000))).toBe('23h ago');
  });

  it('counts days below a month', () => {
    expect(timeAgo(agoIso(MS_PER_DAY))).toBe('1d ago');
    expect(timeAgo(agoIso(14 * MS_PER_DAY))).toBe('14d ago');
    expect(timeAgo(agoIso(29 * MS_PER_DAY))).toBe('29d ago');
  });

  it('coarsens to months and years', () => {
    expect(timeAgo(agoIso(30 * MS_PER_DAY))).toBe('1mo ago');
    expect(timeAgo(agoIso(200 * MS_PER_DAY))).toBe('6mo ago');
    expect(timeAgo(agoIso(400 * MS_PER_DAY))).toBe('1y ago');
  });

  it('returns "unknown" for an unparseable timestamp', () => {
    expect(timeAgo('not-a-date')).toBe('unknown');
    expect(timeAgo('')).toBe('unknown');
  });
});

describe('staleLabel', () => {
  it('reads sub-minute staleness as "just now"', () => {
    expect(staleLabel(0)).toBe('as of just now');
    expect(staleLabel(59)).toBe('as of just now');
  });

  it('counts minutes, then hours, then days', () => {
    expect(staleLabel(60)).toBe('as of 1 min ago');
    expect(staleLabel(59 * 60)).toBe('as of 59 min ago');
    expect(staleLabel(3600)).toBe('as of 1h ago');
    expect(staleLabel(23 * 3600)).toBe('as of 23h ago');
    expect(staleLabel(24 * 3600)).toBe('as of 1d ago');
    expect(staleLabel(3 * 24 * 3600)).toBe('as of 3d ago');
  });

  it('does not print NaN for a non-finite age', () => {
    expect(staleLabel(Number.NaN)).toBe('as of just now');
  });
});

describe('formatTimeleft', () => {
  it('omits an absent or unparseable duration rather than printing NaN', () => {
    expect(formatTimeleft(null)).toBeNull();
    expect(formatTimeleft(undefined)).toBeNull();
    expect(formatTimeleft('')).toBeNull();
    expect(formatTimeleft('soon')).toBeNull();
    expect(formatTimeleft('12:30')).toBeNull();
    expect(formatTimeleft('x.01:00:00')).toBeNull();
  });

  it('formats the plain HH:MM:SS form', () => {
    expect(formatTimeleft('01:23:45')).toBe('1h 23m');
    expect(formatTimeleft('00:07:30')).toBe('7m');
    expect(formatTimeleft('00:00:42')).toBe('42s');
  });

  it('formats the day-prefixed form the arrs emit past 24 hours', () => {
    // "1.02:00:00" is longer than "23:00:00" despite sorting before it.
    expect(formatTimeleft('1.02:00:00')).toBe('1d 2h');
    expect(formatTimeleft('12.00:30:00')).toBe('12d 0h');
  });
});

describe('daysLeft', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns the full window for a just-flagged item', () => {
    expect(daysLeft(agoIso(0), 14)).toBe(14);
  });

  it('counts the window down in whole days, rounding up a partial day', () => {
    expect(daysLeft(agoIso(MS_PER_DAY), 14)).toBe(13);
    expect(daysLeft(agoIso(13 * MS_PER_DAY), 14)).toBe(1);
    expect(daysLeft(agoIso(13.5 * MS_PER_DAY), 14)).toBe(1);
  });

  it('clamps an elapsed window at 0 instead of going negative', () => {
    // The hourly sweep may not have moved the row to pending_approval yet.
    expect(daysLeft(agoIso(14 * MS_PER_DAY), 14)).toBe(0);
    expect(daysLeft(agoIso(90 * MS_PER_DAY), 14)).toBe(0);
  });

  it('honours a non-default window length', () => {
    expect(daysLeft(agoIso(2 * MS_PER_DAY), 7)).toBe(5);
    expect(daysLeft(agoIso(0), 1)).toBe(1);
  });

  it('returns 0 for an unparseable timestamp', () => {
    expect(daysLeft('not-a-date', 14)).toBe(0);
  });
});

describe('posterUrl', () => {
  it('builds a sized TMDB URL from a relative path', () => {
    expect(posterUrl('/abc123.jpg')).toBe('https://image.tmdb.org/t/p/w342/abc123.jpg');
  });

  it('takes an explicit size', () => {
    expect(posterUrl('/abc123.jpg', 'w780')).toBe('https://image.tmdb.org/t/p/w780/abc123.jpg');
  });

  it('tolerates a path with no leading slash', () => {
    expect(posterUrl('abc123.jpg')).toBe('https://image.tmdb.org/t/p/w342/abc123.jpg');
  });

  it('is null for a title with no artwork', () => {
    expect(posterUrl(null)).toBeNull();
    expect(posterUrl(undefined)).toBeNull();
    expect(posterUrl('')).toBeNull();
  });
});
