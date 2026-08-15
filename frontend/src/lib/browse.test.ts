import { describe, expect, it } from 'vitest';

import { DEFAULT_FILTERS, applyRequested, browseQuery, cardKey, mergeCards, narrow } from './browse';
import type { BrowseFilters, DiscoverCard } from './types';

const base: BrowseFilters = { ...DEFAULT_FILTERS };

const card = (overrides: Partial<DiscoverCard>): DiscoverCard => ({
  tmdb_id: 1,
  title: 'Untitled',
  year: 2020,
  media_type: 'movie',
  poster_path: null,
  overview: '',
  rating: null,
  status: null,
  availability: 'requestable',
  ...overrides,
});

describe('narrow', () => {
  it('applies the patch', () => {
    expect(narrow(base, { genre: 27 }).genre).toBe(27);
  });

  it('leaves trending when any filter is touched', () => {
    const trending: BrowseFilters = { ...base, sort: 'trending' };
    expect(narrow(trending, { genre: 27 }).sort).toBe('popular');
    expect(narrow(trending, { media: 'tv' }).sort).toBe('popular');
    expect(narrow(trending, { decade: '2010s' }).sort).toBe('popular');
    expect(narrow(trending, { minRating: 8 }).sort).toBe('popular');
  });

  it('does not override an explicit sort change', () => {
    const trending: BrowseFilters = { ...base, sort: 'trending' };
    expect(narrow(trending, { sort: 'top_rated' }).sort).toBe('top_rated');
    expect(narrow(base, { sort: 'trending' }).sort).toBe('trending');
  });

  it('clears the genre when the media type changes', () => {
    // Movie 28 (Action) is not a TV genre; TV Action is 10759.
    const withGenre: BrowseFilters = { ...base, genre: 28 };
    expect(narrow(withGenre, { media: 'tv' }).genre).toBeNull();
  });

  it('clears genre, decade, and rating when the user picks trending', () => {
    const narrowed: BrowseFilters = { ...base, genre: 27, decade: '2010s', minRating: 8 };
    const next = narrow(narrowed, { sort: 'trending' });
    expect(next.sort).toBe('trending');
    expect(next.genre).toBeNull();
    expect(next.decade).toBeNull();
    expect(next.minRating).toBeNull();
  });
});

describe('browseQuery', () => {
  it('sends sort and page for a plain popular browse', () => {
    expect(browseQuery(base, 1)).toBe('/api/discover/browse?sort=popular&page=1&media=movie');
  });

  it('includes only the filters that are set', () => {
    const f: BrowseFilters = { ...base, media: 'tv', genre: 18, decade: '2010s', minRating: 7 };
    const q = browseQuery(f, 3);
    expect(q).toContain('sort=popular');
    expect(q).toContain('page=3');
    expect(q).toContain('media=tv');
    expect(q).toContain('genre=18');
    expect(q).toContain('decade=2010s');
    expect(q).toContain('min_rating=7');
  });

  it('omits every filter on trending, so the backend 400 can never fire', () => {
    const f: BrowseFilters = { ...base, sort: 'trending', media: 'tv', genre: 27, decade: '2010s', minRating: 8 };
    expect(browseQuery(f, 2)).toBe('/api/discover/browse?sort=trending&page=2');
  });
});

describe('mergeCards', () => {
  it('drops items from the new page that already appear in the existing list', () => {
    // TMDB's trending endpoint genuinely repeats titles across adjacent pages
    // (measured live: 6 dupes in 100 items over 5 pages). If this dedupe were
    // removed — i.e. `mergeCards` went back to a plain `[...existing, ...incoming]`
    // — this assertion would fail: the merged list would contain `tv-30984` twice
    // and its length would be 4, not 3.
    const existing = [card({ tmdb_id: 1, media_type: 'tv' }), card({ tmdb_id: 30984, media_type: 'tv' })];
    const incoming = [card({ tmdb_id: 30984, media_type: 'tv' }), card({ tmdb_id: 2, media_type: 'tv' })];

    const merged = mergeCards(existing, incoming);

    const keys = merged.map(cardKey);
    expect(keys).toEqual(['tv-1', 'tv-30984', 'tv-2']);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('preserves existing order and appends only the genuinely new items', () => {
    const existing = [card({ tmdb_id: 5 }), card({ tmdb_id: 6 })];
    const incoming = [card({ tmdb_id: 6 }), card({ tmdb_id: 7 }), card({ tmdb_id: 8 })];

    expect(mergeCards(existing, incoming).map((c) => c.tmdb_id)).toEqual([5, 6, 7, 8]);
  });

  it('is a no-op append when nothing overlaps', () => {
    const existing = [card({ tmdb_id: 1 })];
    const incoming = [card({ tmdb_id: 2 })];
    expect(mergeCards(existing, incoming).map((c) => c.tmdb_id)).toEqual([1, 2]);
  });
});

describe('applyRequested', () => {
  it('marks a matching requestable card as requested', () => {
    const items = [card({ tmdb_id: 42, availability: 'requestable' })];
    const result = applyRequested(items, new Set([42]));
    expect(result[0].availability).toBe('requested');
  });

  it('leaves cards alone when their id is not in the requested set', () => {
    const items = [card({ tmdb_id: 42, availability: 'requestable' })];
    const result = applyRequested(items, new Set([7]));
    expect(result[0].availability).toBe('requestable');
  });

  it('does not downgrade a card that is already available', () => {
    const items = [card({ tmdb_id: 42, availability: 'available' })];
    const result = applyRequested(items, new Set([42]));
    expect(result[0].availability).toBe('available');
  });

  it('returns the same array reference when the requested set is empty', () => {
    const items = [card({ tmdb_id: 42 })];
    expect(applyRequested(items, new Set())).toBe(items);
  });
});
