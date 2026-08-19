import { describe, expect, it } from 'vitest';

import { isPerson, moveActive, suggestKey } from './suggest';
import type { DiscoverCard, PersonSuggestion } from './types';

const card = (over: Partial<DiscoverCard> = {}): DiscoverCard => ({
  tmdb_id: 603,
  title: 'The Matrix',
  year: 1999,
  media_type: 'movie',
  poster_path: '/m.jpg',
  overview: '',
  rating: 8.2,
  status: null,
  availability: 'requestable',
  ...over,
});

const person = (over: Partial<PersonSuggestion> = {}): PersonSuggestion => ({
  person_id: 31,
  name: 'Tom Hanks',
  profile_path: '/p.jpg',
  media_type: 'person',
  ...over,
});

describe('isPerson', () => {
  it('narrows a person row', () => {
    expect(isPerson(person())).toBe(true);
  });

  it('leaves title rows alone', () => {
    expect(isPerson(card())).toBe(false);
    expect(isPerson(card({ media_type: 'tv' }))).toBe(false);
  });
});

describe('suggestKey', () => {
  it('namespaces by kind', () => {
    expect(suggestKey(card())).toBe('movie-603');
    expect(suggestKey(person())).toBe('person-31');
  });

  it('keeps a person and a title with the same id apart', () => {
    expect(suggestKey(card({ tmdb_id: 13 }))).not.toBe(suggestKey(person({ person_id: 13 })));
  });

  it('keeps a film and a show with the same id apart', () => {
    expect(suggestKey(card({ tmdb_id: 13 }))).not.toBe(
      suggestKey(card({ tmdb_id: 13, media_type: 'tv' })),
    );
  });
});

describe('moveActive', () => {
  it('starts at the top from nothing highlighted', () => {
    expect(moveActive(-1, 1, 3)).toBe(0);
  });

  it('reaches the bottom by going up from nothing highlighted', () => {
    expect(moveActive(-1, -1, 3)).toBe(2);
  });

  it('steps', () => {
    expect(moveActive(0, 1, 3)).toBe(1);
    expect(moveActive(2, -1, 3)).toBe(1);
  });

  it('wraps at both ends', () => {
    expect(moveActive(2, 1, 3)).toBe(0);
    expect(moveActive(0, -1, 3)).toBe(2);
  });

  it('has nowhere to go in an empty list', () => {
    expect(moveActive(-1, 1, 0)).toBe(-1);
    expect(moveActive(0, -1, 0)).toBe(-1);
  });
});
