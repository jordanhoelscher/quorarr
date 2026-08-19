/**
 * Pure helpers for the search dropdown.
 *
 * Out of the component so the two things that are easy to get subtly wrong —
 * telling a person row from a title row, and where the arrow keys land — can
 * be tested without mounting anything.
 */

import type { PersonSuggestion, Suggestion } from './types';

/**
 * Narrow a suggestion to a person.
 *
 * The discriminant is `media_type`, which the backend sets to the literal
 * `"person"`; nothing else in the app produces that value, so this is the one
 * check the rendering and the tap handler both branch on.
 */
export const isPerson = (row: Suggestion): row is PersonSuggestion =>
  row.media_type === 'person';

/**
 * The `{#each}` key for a suggestion row.
 *
 * Namespaced by kind because the two id spaces overlap freely — TMDB person
 * 13 and TMDB movie 13 both exist — and a collision is a Svelte 5
 * `each_key_duplicate` crash in production, not a cosmetic glitch.
 */
export const suggestKey = (row: Suggestion): string =>
  isPerson(row) ? `person-${row.person_id}` : `${row.media_type}-${row.tmdb_id}`;

/**
 * Where an arrow key moves the highlight.
 *
 * `-1` means nothing is highlighted, which is the state the list opens in:
 * Enter then submits the typed query rather than picking whatever happened to
 * be first, so a user who types a full title and hits Enter gets their own
 * words and not the dropdown's guess. Both ends wrap, and from `-1` an Up
 * arrow lands on the last row.
 */
export const moveActive = (current: number, delta: number, count: number): number => {
  if (count <= 0) return -1;
  const next = current + delta;
  if (next < 0) return count - 1;
  if (next >= count) return 0;
  return next;
};
