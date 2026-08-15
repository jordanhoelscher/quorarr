<script lang="ts">
  /**
   * The browse page's controls: sort, type, genre, decade, minimum rating.
   *
   * The bar is dumb on purpose. It reports a patch and re-renders from the
   * filters it is handed; the rule about what a patch *means* (leaving
   * trending, clearing a stale genre) lives in `narrow`, so it is unit
   * tested rather than tangled with markup.
   *
   * On trending every control is disabled, because that endpoint accepts no
   * narrowing at all — not even a media type. Disabling is honest where
   * hiding would be baffling: the controls are still there, visibly
   * unavailable, with a line saying why.
   */
  import type { BrowseDecade, BrowseFilters, BrowseSort, Genre } from './types';

  interface Props {
    filters: BrowseFilters;
    genres: Genre[];
    onchange: (patch: Partial<BrowseFilters>) => void;
  }

  const { filters, genres, onchange }: Props = $props();

  const SORTS: readonly { id: BrowseSort; label: string }[] = [
    { id: 'trending', label: 'Trending' },
    { id: 'popular', label: 'Popular' },
    { id: 'newest', label: 'Newest' },
    { id: 'upcoming', label: 'Coming soon' },
    { id: 'top_rated', label: 'Highest rated' },
  ];

  const DECADES: readonly { id: BrowseDecade; label: string }[] = [
    { id: '2020s', label: '2020s' },
    { id: '2010s', label: '2010s' },
    { id: '2000s', label: '2000s' },
    { id: '1990s', label: '90s' },
    { id: 'older', label: 'Older' },
  ];

  const locked = $derived(filters.sort === 'trending');

  /** Chips toggle: tapping the active one clears it. */
  const toggleDecade = (id: BrowseDecade): void =>
    onchange({ decade: filters.decade === id ? null : id });

  const toggleRating = (value: 7 | 8): void =>
    onchange({ minRating: filters.minRating === value ? null : value });
</script>

<div class="bar">
  <label class="control">
    <span class="label mono">Sort</span>
    <select
      class="field"
      value={filters.sort}
      onchange={(e) => onchange({ sort: e.currentTarget.value as BrowseSort })}
    >
      {#each SORTS as option (option.id)}
        <option value={option.id}>{option.label}</option>
      {/each}
    </select>
  </label>

  <div class="segmented" role="group" aria-label="Media type">
    <button
      class="seg"
      class:on={!locked && filters.media === 'movie'}
      aria-pressed={!locked && filters.media === 'movie'}
      disabled={locked}
      onclick={() => onchange({ media: 'movie' })}>Movies</button
    >
    <button
      class="seg"
      class:on={!locked && filters.media === 'tv'}
      aria-pressed={!locked && filters.media === 'tv'}
      disabled={locked}
      onclick={() => onchange({ media: 'tv' })}>TV</button
    >
  </div>

  <label class="control">
    <span class="label mono">Genre</span>
    <select
      class="field"
      disabled={locked}
      value={filters.genre === null ? '' : String(filters.genre)}
      onchange={(e) =>
        onchange({ genre: e.currentTarget.value === '' ? null : Number(e.currentTarget.value) })}
    >
      <option value="">Any genre</option>
      {#each genres as genre (genre.id)}
        <option value={String(genre.id)}>{genre.name}</option>
      {/each}
    </select>
  </label>

  <div class="chips" role="group" aria-label="Decade">
    {#each DECADES as decade (decade.id)}
      <button
        class="chip"
        class:on={!locked && filters.decade === decade.id}
        aria-pressed={!locked && filters.decade === decade.id}
        disabled={locked}
        onclick={() => toggleDecade(decade.id)}>{decade.label}</button
      >
    {/each}
  </div>

  <div class="chips" role="group" aria-label="Minimum rating">
    {#each [7, 8] as value (value)}
      <button
        class="chip"
        class:on={!locked && filters.minRating === value}
        aria-pressed={!locked && filters.minRating === value}
        disabled={locked}
        onclick={() => toggleRating(value as 7 | 8)}>{value}+</button
      >
    {/each}
  </div>

  {#if locked}
    <p class="note">Trending is TMDB's own list — it can't be narrowed. Pick another sort to filter.</p>
  {/if}
</div>

<style>
  .bar {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--sp-3);
    margin-bottom: var(--sp-5);
  }

  .control {
    display: flex;
    flex-direction: column;
    gap: var(--sp-1);
  }

  .label {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .field {
    padding: var(--sp-2) var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    background: var(--ink-raised);
    color: var(--vapor);
    font: inherit;
    /* 16px floor — a <select> zooms iOS just as a text input does. */
    font-size: var(--fs-field);
    transition: border-color var(--dur-fast) var(--ease);
  }

  .field:focus {
    border-color: var(--edge-glow);
    outline: none;
  }

  .field option {
    background: var(--basin);
    color: var(--vapor);
  }

  .segmented {
    display: flex;
    padding: 3px;
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    background: var(--ink-raised);
  }

  .seg,
  .chip {
    padding: var(--sp-2) var(--sp-4);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-sm);
    transition:
      color var(--dur-fast) var(--ease),
      background var(--dur-fast) var(--ease);
  }

  .chip {
    border: 1px solid var(--edge);
    background: var(--ink-raised);
  }

  .chips {
    display: flex;
    gap: var(--sp-2);
  }

  .seg.on,
  .chip.on {
    background: var(--memory-wash);
    box-shadow: inset 0 0 0 1px rgb(var(--memory-rgb) / 45%);
    color: var(--memory);
  }

  .seg:disabled,
  .chip:disabled,
  .field:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .note {
    flex-basis: 100%;
    margin: 0;
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }
</style>
