<script lang="ts">
  /**
   * One show, season by season.
   *
   * Opens instantly with the row the list already has, then fetches the detail
   * endpoint for the per-season quality mix — the one thing the list can't
   * carry, since it costs an extra Sonarr fetch per series. If that fetch
   * fails the seasons still render; only the quality chips are missing, and
   * the view says so rather than silently looking complete.
   */
  import { onMount } from 'svelte';

  import Poster from '../lib/Poster.svelte';
  import { ApiError, api } from '../lib/api';
  import { formatBytes } from '../lib/format';
  import type { ActionTarget, SeriesRow } from '../lib/types';

  interface Props {
    /** The list row — header content while the detail fetch is in flight. */
    row: SeriesRow;
    onclose: () => void;
    onaction: (target: ActionTarget) => void;
    /** False while an action sheet sits on top and owns the Escape key. */
    escapable?: boolean;
  }

  const { row, onclose, onaction, escapable = true }: Props = $props();

  let detail = $state<SeriesRow | null>(null);
  let detailFailed = $state(false);

  onMount(async () => {
    try {
      detail = await api.get<SeriesRow>(`/api/library/series/${row.arr_id}`);
    } catch (err) {
      // A 401 has already dropped the whole app to the login screen.
      if (!(err instanceof ApiError && err.status === 401)) detailFailed = true;
    }
  });

  const shown = $derived(detail ?? row);

  const seriesTarget = (): ActionTarget => ({
    kind: 'series',
    arrId: row.arr_id,
    title: row.title,
    sizeBytes: row.size_bytes,
  });

  const seasonTarget = (seasonNumber: number, sizeBytes: number): ActionTarget => ({
    kind: 'season',
    arrId: row.arr_id,
    title: `${row.title} — Season ${seasonNumber}`,
    sizeBytes,
    seasonNumber,
  });

  /** Biggest quality bucket first, so the dominant format reads at a glance. */
  const qualityChips = (qualities: Record<string, number> | undefined): [string, number][] =>
    Object.entries(qualities ?? {}).sort((a, b) => b[1] - a[1]);
</script>

<svelte:window onkeydown={(event) => escapable && event.key === 'Escape' && onclose()} />

<div class="overlay" role="dialog" aria-modal="true" aria-label={row.title}>
  <div class="sheet">
    <div class="bar">
      <button class="back" onclick={onclose}>
        <span aria-hidden="true">‹</span> Library
      </button>
    </div>

    <header class="head">
      <Poster src={shown.poster} size="lead" />

      <div class="ident">
        <h3>{shown.title}</h3>
        <p class="facts mono">
          {#if shown.year}<span>{shown.year}</span><span class="sep">·</span>{/if}
          <span>{shown.seasons.length} seasons</span>
          <span class="sep">·</span>
          <span>{shown.episode_count} episodes</span>
        </p>
        <p class="total mono">{formatBytes(shown.size_bytes)}</p>

        <button class="series-action" onclick={() => onaction(seriesTarget())}>
          Actions for the whole series
        </button>
      </div>
    </header>

    {#if detailFailed}
      <p class="notice mono">Per-season quality is unavailable right now.</p>
    {/if}

    <ul class="seasons">
      {#each shown.seasons as season (season.season_number)}
        <li class="season">
          <div class="season-top">
            <span class="season-name">
              <span
                class="monitor"
                class:on={season.monitored}
                title={season.monitored ? 'Monitored' : 'Not monitored'}
              ></span>
              {season.season_number === 0 ? 'Specials' : `Season ${season.season_number}`}
            </span>

            <span class="season-size mono">{formatBytes(season.size_bytes)}</span>

            <button
              class="season-action"
              aria-label="Actions for season {season.season_number}"
              onclick={() => onaction(seasonTarget(season.season_number, season.size_bytes))}
            >
              ⋯
            </button>
          </div>

          <div class="season-meta">
            <span class="episodes mono">{season.episode_file_count} files</span>

            {#each qualityChips(season.qualities) as [name, count] (name)}
              <span class="qchip mono">{name}<span class="qcount">{count}</span></span>
            {/each}
          </div>
        </li>
      {/each}
    </ul>
  </div>
</div>

<style>
  .overlay {
    position: fixed;
    inset: 0;
    z-index: 40;
    overflow-y: auto;
    overscroll-behavior: contain;
    background:
      radial-gradient(110% 60% at 80% -8%, rgb(var(--memory-rgb) / 9%), transparent 60%),
      var(--ink);
    animation: rise var(--dur) var(--ease) both;
  }

  .sheet {
    max-width: var(--shell-max);
    margin-inline: auto;
    padding: var(--sp-4) var(--sp-4) calc(var(--sp-8) + env(safe-area-inset-bottom));
  }

  .bar {
    padding-block: var(--sp-2) var(--sp-5);
  }

  .back {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-2);
    padding: var(--sp-2) var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-sm);
    transition:
      color var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease);
  }

  .back:hover {
    color: var(--vapor);
    border-color: var(--edge-hi);
  }

  /* --------------------------------------------------------------- head */

  .head {
    display: flex;
    gap: var(--sp-5);
    padding-bottom: var(--sp-5);
    border-bottom: 1px solid var(--edge);
  }

  .ident {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--sp-2);
    min-width: 0;
  }

  h3 {
    font-size: var(--fs-lg);
    text-wrap: balance;
  }

  .facts {
    display: flex;
    gap: var(--sp-2);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .sep {
    opacity: 0.4;
  }

  .total {
    color: var(--memory);
    font-size: var(--fs-md);
  }

  .series-action {
    margin-top: var(--sp-2);
    padding: var(--sp-2) var(--sp-4);
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-full);
    color: var(--vapor);
    font-size: var(--fs-sm);
    transition:
      border-color var(--dur-fast) var(--ease),
      background-color var(--dur-fast) var(--ease);
  }

  .series-action:hover {
    border-color: var(--edge-glow);
    background: var(--memory-wash);
  }

  .notice {
    margin-top: var(--sp-4);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
  }

  /* ------------------------------------------------------------ seasons */

  .seasons {
    margin: var(--sp-5) 0 0;
    padding: 0;
    list-style: none;
  }

  .season {
    padding: var(--sp-4) 0;
    border-bottom: 1px solid var(--edge);
  }

  .season-top {
    display: flex;
    align-items: baseline;
    gap: var(--sp-3);
  }

  .season-name {
    display: flex;
    align-items: baseline;
    gap: var(--sp-3);
    flex: 1;
    min-width: 0;
    color: var(--vapor);
    font-size: var(--fs-base);
  }

  /* A monitored season is lit; an unmonitored one is a hollow socket. */
  .monitor {
    flex: none;
    width: 7px;
    height: 7px;
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-full);
  }

  .monitor.on {
    border-color: transparent;
    background: var(--memory);
    box-shadow: var(--glow-memory);
  }

  .season-size {
    flex: none;
    color: var(--rune);
    font-size: var(--fs-sm);
  }

  .season-action {
    flex: none;
    width: 2rem;
    height: 2rem;
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    line-height: 1;
    transition:
      color var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease);
  }

  .season-action:hover {
    color: var(--vapor);
    border-color: var(--edge-glow);
  }

  .season-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--sp-2);
    margin-top: var(--sp-3);
    padding-left: calc(7px + var(--sp-3));
  }

  .episodes {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
  }

  .qchip {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-2);
    padding: 1px var(--sp-2);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-micro);
  }

  .qcount {
    color: var(--memory);
  }

  @media (width < 30rem) {
    .head {
      gap: var(--sp-4);
    }
  }
</style>
