/**
 * Pure helpers for the browse page's filter state.
 *
 * Kept out of the component so the two rules that are easy to get subtly
 * wrong — leaving trending, and what actually reaches the query string —
 * can be tested without mounting anything.
 */

import type { BrowseFilters, DiscoverCard } from './types';

export const DEFAULT_FILTERS: BrowseFilters = {
  sort: 'popular',
  media: 'movie',
  genre: null,
  decade: null,
  minRating: null,
};

/**
 * Apply a filter change, honouring the two rules the UI must never break.
 *
 * Trending is a distinct upstream endpoint that accepts no narrowing at all,
 * so touching any filter moves the sort to Popular rather than presenting a
 * filtered-looking list that was never filtered. An explicit `sort` in the
 * patch always wins — that is the user choosing, not narrowing.
 *
 * Changing media clears the genre: the movie and TV vocabularies are
 * different id spaces (film Action is 28, TV Action & Adventure is 10759),
 * so a carried-over id would silently filter by something else entirely.
 *
 * Moving *to* trending clears genre/decade/minRating so stored state matches
 * applied state: `browseQuery` already omits them on trending, but leaving
 * the values sitting in `filters` meant the bar rendered stale chips as off
 * while the `<select>` kept showing the old genre — and picking Popular
 * again silently reapplied filters the user never saw as active.
 */
export const narrow = (filters: BrowseFilters, patch: Partial<BrowseFilters>): BrowseFilters => {
  const next: BrowseFilters = { ...filters, ...patch };

  if (patch.sort === undefined && filters.sort === 'trending') next.sort = 'popular';
  if (patch.media !== undefined && patch.media !== filters.media) next.genre = null;
  if (patch.sort === 'trending') {
    next.genre = null;
    next.decade = null;
    next.minRating = null;
  }

  return next;
};

/**
 * The request URL for one page.
 *
 * On trending every filter is omitted, so the backend's "trending cannot be
 * narrowed" 400 is unreachable from this client. That 400 exists for a stale
 * or hand-rolled caller, not for this one.
 */
export const browseQuery = (filters: BrowseFilters, page: number): string => {
  const params = new URLSearchParams({ sort: filters.sort, page: String(page) });

  if (filters.sort !== 'trending') {
    params.set('media', filters.media);
    if (filters.genre !== null) params.set('genre', String(filters.genre));
    if (filters.decade !== null) params.set('decade', filters.decade);
    if (filters.minRating !== null) params.set('min_rating', String(filters.minRating));
  }

  return `/api/discover/browse?${params}`;
};

/**
 * The identity a browse/discover card is keyed by, everywhere: the `{#each}`
 * blocks and `mergeCards` below must use this exact expression, not a copy
 * of it, or the two can silently drift.
 */
export const cardKey = (card: DiscoverCard): string => `${card.media_type}-${card.tmdb_id}`;

/**
 * Append a page of results, dropping anything already present.
 *
 * TMDB's trending endpoint (and, less often, the other sorts) repeats titles
 * across adjacent pages. Svelte 5 throws `each_key_duplicate` on a duplicate
 * `{#each}` key in production builds, not just dev, so an unguarded append
 * eventually kills the view outright. Existing items are kept in place and
 * new arrivals are appended after them, so scroll position never jumps.
 */
export const mergeCards = (existing: DiscoverCard[], incoming: DiscoverCard[]): DiscoverCard[] => {
  const seen = new Set(existing.map(cardKey));
  const fresh: DiscoverCard[] = [];
  for (const card of incoming) {
    const key = cardKey(card);
    if (seen.has(key)) continue;
    seen.add(key);
    fresh.push(card);
  }
  return [...existing, ...fresh];
};

/**
 * Reflect requests made elsewhere onto a browse page's cards.
 *
 * The browse grid holds its own copy of cards, separate from Discover's
 * `results`/`shelves` — and the browse response is cached 300s server-side,
 * so a reload wouldn't pick up a fresh availability either. Deriving here
 * (rather than mutating `items` in place) keeps that cache irrelevant: the
 * badge is correct regardless of what the last fetch returned.
 */
export const applyRequested = (
  items: DiscoverCard[],
  requested: ReadonlySet<number>,
): DiscoverCard[] => {
  if (requested.size === 0) return items;
  return items.map((card) =>
    requested.has(card.tmdb_id) && card.availability === 'requestable'
      ? { ...card, availability: 'requested' }
      : card,
  );
};
