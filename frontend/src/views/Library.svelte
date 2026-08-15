<script lang="ts">
  /**
   * Everything on the server, sorted biggest-first.
   *
   * Biggest-first is the default on purpose: the reason a friend opens this
   * list is almost always to find something worth giving back, and the
   * fastest way to free 400 GB is to look at the top of the list.
   *
   * Both lists are held in memory and filtered client-side — Radarr/Sonarr
   * return the whole library in one response anyway, so a search box that
   * round-trips would be slower and no more correct. Rows are revealed in
   * pages rather than all at once, which keeps a few thousand posters from
   * being laid out on a phone in one frame.
   */
  import { onMount } from 'svelte';
  import { unreachable } from '../lib/branding.svelte';

  import ActionSheet from '../lib/ActionSheet.svelte';
  import Placeholder from '../lib/Placeholder.svelte';
  import Poster from '../lib/Poster.svelte';
  import ViewHead from '../lib/ViewHead.svelte';
  import { ApiError, api } from '../lib/api';
  import { formatBytes } from '../lib/format';
  import { toasts } from '../lib/toast.svelte';
  import type { ActionTarget, LibraryList, MovieRow, SeriesRow } from '../lib/types';
  import SeriesDetail from './SeriesDetail.svelte';

  type Kind = 'movie' | 'series';
  type SortBy = 'size' | 'added' | 'title';

  /** Rows revealed per page. */
  const PAGE = 60;

  let kind = $state<Kind>('movie');
  let query = $state('');
  let sortBy = $state<SortBy>('size');
  let visible = $state(PAGE);

  let movies = $state<MovieRow[] | null>(null);
  let series = $state<SeriesRow[] | null>(null);
  let loading = $state(true);
  let refreshing = $state(false);
  let error = $state<string | null>(null);

  /** Open overlays. Only one action sheet exists, shared with the detail view. */
  let openSeriesId = $state<number | null>(null);
  let actionTarget = $state<ActionTarget | null>(null);

  const load = async (which: Kind): Promise<void> => {
    loading = true;
    error = null;
    try {
      if (which === 'movie') {
        movies = (await api.get<LibraryList<MovieRow>>('/api/library/movies')).items;
      } else {
        series = (await api.get<LibraryList<SeriesRow>>('/api/library/series')).items;
      }
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        error = err instanceof ApiError ? err.message : unreachable();
      }
    } finally {
      loading = false;
    }
  };

  onMount(() => void load('movie'));

  const show = (next: Kind): void => {
    if (kind === next) return;
    kind = next;
    visible = PAGE;
    error = null;
    if (next === 'movie' ? movies === null : series === null) void load(next);
  };

  /** Drop the backend's cached arr responses, then refetch this tab live. */
  const refresh = async (): Promise<void> => {
    if (refreshing) return;
    refreshing = true;
    try {
      await api.post('/api/library/refresh');
      movies = null;
      series = null;
      await load(kind);
      // `load` swallows its own failure into `error` rather than throwing, so
      // toasting unconditionally here would claim success over an empty view.
      if (error === null) toasts.push('Library refreshed');
    } catch (err) {
      toasts.push(
        err instanceof ApiError ? err.message : 'Could not refresh the library.',
        'warn',
      );
    } finally {
      refreshing = false;
    }
  };

  const search = (event: Event & { currentTarget: HTMLInputElement }): void => {
    query = event.currentTarget.value;
    visible = PAGE;
  };

  const reorder = (event: Event & { currentTarget: HTMLSelectElement }): void => {
    sortBy = event.currentTarget.value as SortBy;
    visible = PAGE;
  };

  const rows = $derived.by((): (MovieRow | SeriesRow)[] => {
    const source: (MovieRow | SeriesRow)[] | null = kind === 'movie' ? movies : series;
    if (!source) return [];

    const needle = query.trim().toLowerCase();
    const matched = needle
      ? source.filter((row) => row.title.toLowerCase().includes(needle))
      : source;

    const sorted = [...matched];
    if (sortBy === 'title') {
      sorted.sort((a, b) => a.title.localeCompare(b.title));
    } else if (sortBy === 'added') {
      // Newest first; a row with no `added` sorts to the bottom rather than
      // to 1970 pretending to be the oldest thing in the library.
      sorted.sort((a, b) => (Date.parse(b.added ?? '') || 0) - (Date.parse(a.added ?? '') || 0));
    } else {
      sorted.sort((a, b) => b.size_bytes - a.size_bytes);
    }
    return sorted;
  });

  const shown = $derived(rows.slice(0, visible));
  const openDetail = $derived(
    openSeriesId === null ? null : (series?.find((s) => s.arr_id === openSeriesId) ?? null),
  );

  const pick = (row: MovieRow | SeriesRow): void => {
    if (row.media_type === 'movie') {
      actionTarget = {
        kind: 'movie',
        arrId: row.arr_id,
        title: row.title,
        sizeBytes: row.size_bytes,
        resolution: row.resolution,
        quality: row.quality,
      };
    } else {
      openSeriesId = row.arr_id;
    }
  };
</script>

<section class="view">
  <ViewHead eyebrow="On the server" title="Library">
    {#snippet aside()}
      <button class="refresh" disabled={refreshing} onclick={refresh}>
        <svg class="refresh-icon" class:spinning={refreshing} viewBox="0 0 24 24" aria-hidden="true">
          <path d="M20 12a8 8 0 1 1-2.34-5.66M20 4v5h-5" />
        </svg>
        <span class="refresh-label">{refreshing ? 'Refreshing' : 'Refresh'}</span>
      </button>
    {/snippet}
  </ViewHead>

  <div class="controls">
    <div class="toggle" role="group" aria-label="Library type">
      <button class="seg" class:on={kind === 'movie'} onclick={() => show('movie')}>Movies</button>
      <button class="seg" class:on={kind === 'series'} onclick={() => show('series')}>TV</button>
    </div>

    <input
      class="search"
      type="search"
      placeholder="Search titles"
      aria-label="Search titles"
      value={query}
      oninput={search}
    />

    <select class="sort" aria-label="Sort by" value={sortBy} onchange={reorder}>
      <option value="size">Largest first</option>
      <option value="added">Recently added</option>
      <option value="title">A–Z</option>
    </select>
  </div>

  {#if loading}
    <Placeholder tone="loading" message="Opening the library…" />
  {:else if error}
    <Placeholder tone="error" message={error} onretry={() => void load(kind)} />
  {:else if rows.length === 0}
    <Placeholder
      tone="empty"
      message={query.trim()
        ? `Nothing here matches “${query.trim()}”. Search only reads titles, so try a shorter piece of the name — or ask for it in Jellyseerr if it isn’t on the server yet.`
        : kind === 'movie'
          ? 'No films on the server yet. Anything requested and downloaded shows up here.'
          : 'No shows on the server yet. Anything requested and downloaded shows up here.'}
    />
  {:else}
    <p class="tally mono">
      {rows.length}
      {kind === 'movie' ? 'films' : 'series'} · {formatBytes(
        rows.reduce((sum, row) => sum + row.size_bytes, 0),
      )}
    </p>

    <ul class="rows">
      {#each shown as row (row.arr_id)}
        <li>
          <button class="row" onclick={() => pick(row)}>
            <Poster src={row.poster} />

            <span class="body">
              <span class="name">
                <span class="name-text">{row.title}</span>
                {#if row.year}<span class="year mono">{row.year}</span>{/if}
              </span>

              <span class="sub">
                {#if row.media_type === 'movie'}
                  {#if row.quality}
                    <span class="quality mono">{row.quality}</span>
                  {:else}
                    <span class="missing mono">No file</span>
                  {/if}
                {:else}
                  <span class="mono">{row.seasons.length} seasons</span>
                  <span class="dot" aria-hidden="true">·</span>
                  <span class="mono">{row.episode_count} episodes</span>
                {/if}
              </span>
            </span>

            <span class="size mono">{formatBytes(row.size_bytes)}</span>
            <span class="chevron" aria-hidden="true">›</span>
          </button>
        </li>
      {/each}
    </ul>

    {#if shown.length < rows.length}
      <button class="more" onclick={() => (visible += PAGE)}>
        Show {Math.min(PAGE, rows.length - shown.length)} more
        <span class="more-count mono">({shown.length} of {rows.length})</span>
      </button>
    {/if}
  {/if}
</section>

{#if openDetail}
  <SeriesDetail
    row={openDetail}
    escapable={actionTarget === null}
    onclose={() => (openSeriesId = null)}
    onaction={(target) => (actionTarget = target)}
  />
{/if}

{#if actionTarget}
  <ActionSheet target={actionTarget} onclose={() => (actionTarget = null)} />
{/if}

<style>
  .view {
    animation: rise var(--dur-slow) var(--ease) both;
  }

  /* ------------------------------------------------------------ chrome */

  .refresh {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    padding: var(--sp-1) var(--sp-3);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-xs);
    transition:
      color var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease);
  }

  .refresh:hover:not(:disabled) {
    color: var(--vapor);
    border-color: var(--edge-glow);
  }

  .refresh:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .refresh-icon {
    width: 14px;
    height: 14px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.8;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .refresh-icon.spinning {
    animation: swirl 1.1s linear infinite;
  }

  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-2);
    margin-bottom: var(--sp-4);
  }

  .toggle {
    display: flex;
    padding: 3px;
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    background: var(--ink-raised);
  }

  .seg {
    padding: var(--sp-2) var(--sp-4);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-sm);
    transition:
      color var(--dur-fast) var(--ease),
      background-color var(--dur-fast) var(--ease);
  }

  .seg.on {
    background: var(--memory-wash);
    color: var(--memory);
    box-shadow: inset 0 0 0 1px rgb(var(--memory-rgb) / 26%);
  }

  .search,
  .sort {
    padding: var(--sp-2) var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    background: var(--ink-raised);
    color: var(--vapor);
    font: inherit;
    /* 16px floor — a <select> zooms iOS just as a text input does. The row
       these sit in is sized off the control, so it grows to match. */
    font-size: var(--fs-field);
    transition: border-color var(--dur-fast) var(--ease);
  }

  .search {
    flex: 1;
    min-width: 8rem;
  }

  .search::placeholder {
    color: var(--rune-dim);
  }

  .search:focus,
  .sort:focus {
    border-color: var(--edge-glow);
    outline: none;
  }

  .sort {
    appearance: none;
    padding-right: var(--sp-6);
    color: var(--rune);
    /* Chevron drawn in the field so the native control matches the stone. */
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' fill='none' stroke='%239aabbf' stroke-width='1.5'%3E%3Cpath d='M1 1l4 4 4-4'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right var(--sp-4) center;
  }

  .sort option {
    background: var(--basin);
    color: var(--vapor);
  }

  .tally {
    margin-bottom: var(--sp-3);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  /* -------------------------------------------------------------- rows */

  .rows {
    display: flex;
    flex-direction: column;
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: 1px solid var(--edge);
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--sp-4);
    width: 100%;
    padding: var(--sp-3) var(--sp-2);
    border-bottom: 1px solid var(--edge);
    text-align: left;
    transition: background-color var(--dur-fast) var(--ease);
  }

  .row:hover {
    background: rgb(var(--memory-rgb) / 4%);
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: 3px;
    flex: 1;
    min-width: 0;
  }

  .name {
    display: flex;
    align-items: baseline;
    gap: var(--sp-2);
    min-width: 0;
    color: var(--vapor);
    font-size: var(--fs-base);
  }

  /*
   * The truncation has to live on the text itself, not on the flex row:
   * `text-overflow` never applies to a flex container, so putting it on
   * `.name` silently pushed the year out of the row instead of clipping.
   */
  .name-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .year {
    flex: none;
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .sub {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
  }

  .quality {
    padding: 1px var(--sp-2);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    letter-spacing: 0.04em;
  }

  /* No file on disk is a gap, not a failure — stays cold. */
  .missing {
    color: var(--rune-dim);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    opacity: 0.75;
  }

  .dot {
    opacity: 0.4;
  }

  .size {
    flex: none;
    color: var(--rune);
    font-size: var(--fs-sm);
    text-align: right;
  }

  .chevron {
    flex: none;
    color: var(--rune-dim);
    font-size: var(--fs-md);
    line-height: 1;
  }

  .more {
    display: block;
    width: 100%;
    margin-top: var(--sp-4);
    padding: var(--sp-3);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-sm);
    transition:
      border-color var(--dur-fast) var(--ease),
      color var(--dur-fast) var(--ease);
  }

  .more:hover {
    border-color: var(--edge-glow);
    color: var(--vapor);
  }

  .more-count {
    margin-left: var(--sp-2);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
  }

  @media (width < 30rem) {
    .refresh-label {
      display: none;
    }
  }
</style>
