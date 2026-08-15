<script lang="ts">
  /**
   * Everything between "someone asked for it" and "it is watchable".
   *
   * Refreshes itself every 30s, but only while the tab is actually visible —
   * a phone in a pocket should not be polling. A background refresh never
   * flips the view back to its loading state: the numbers move, the layout
   * does not, so watching a download tick along doesn't strobe.
   */
  import { onMount } from 'svelte';
  import { unreachable } from '../lib/branding.svelte';

  import Placeholder from '../lib/Placeholder.svelte';
  import ViewHead from '../lib/ViewHead.svelte';
  import { ApiError, api } from '../lib/api';
  import { formatTimeleft, staleLabel, timeAgo } from '../lib/format';
  import type { PipelineBoard, PipelineCard } from '../lib/types';

  const REFRESH_MS = 30_000;

  let board = $state<PipelineBoard | null>(null);
  let error = $state<string | null>(null);
  let loading = $state(true);

  const load = async (): Promise<void> => {
    try {
      board = await api.get<PipelineBoard>('/api/pipeline');
      error = null;
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        // Keep whatever is already on screen; a failed poll is not a reason
        // to blank a board that was fine ten seconds ago.
        error = err instanceof ApiError ? err.message : unreachable();
      }
    } finally {
      loading = false;
    }
  };

  const retry = (): void => {
    loading = true;
    void load();
  };

  onMount(() => {
    let timer: ReturnType<typeof setInterval> | undefined;

    const stop = (): void => {
      if (timer !== undefined) clearInterval(timer);
      timer = undefined;
    };

    const start = (): void => {
      stop();
      timer = setInterval(() => void load(), REFRESH_MS);
    };

    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') {
        void load();
        start();
      } else {
        stop();
      }
    };

    void load();
    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  });

  /**
   * Jellyseerr carries no title of its own until something hits the queue.
   *
   * `media_type` arrives lowercase from upstream, so it gets capitalised for
   * display — except "tv", which is an initialism and would otherwise read as
   * "Tv request by Sam".
   */
  const MEDIA_WORDS: Record<string, string> = { tv: 'TV' };

  const cardTitle = (card: PipelineCard): string => {
    if (card.title) return card.title;
    const kind = card.media_type || 'media';
    const word = MEDIA_WORDS[kind] ?? `${kind.charAt(0).toUpperCase()}${kind.slice(1)}`;
    return `${word} request`;
  };

  const STATUS_WORDS: Record<string, string> = {
    requested: 'Requested',
    processing: 'Processing',
    partially_available: 'Partly there',
    available: 'Ready',
    downloading: 'Downloading',
    unknown: 'Unknown',
  };

  const statusWord = (card: PipelineCard): string => STATUS_WORDS[card.status] ?? card.status;

  const countLabel = (card: PipelineCard): string | null => {
    if (!card.count) return null;
    if (card.media_type === 'movie') return card.count > 1 ? `${card.count} files` : '1 file';
    return card.count > 1 ? `${card.count} episodes` : '1 episode';
  };
</script>

<section class="view">
  <ViewHead eyebrow="In flight" title="Pipeline">
    {#snippet aside()}
      {#if board?.stale_seconds !== undefined}
        <span class="stale mono">{staleLabel(board.stale_seconds)}</span>
      {:else if board}
        <span class="live mono"><span class="live-dot" aria-hidden="true"></span>Live</span>
      {/if}
    {/snippet}
  </ViewHead>

  {#if loading && !board}
    <Placeholder tone="loading" message="Checking what is moving…" />
  {:else if error && !board}
    <Placeholder tone="error" message={error} onretry={retry} />
  {:else if board}
    {#if error}
      <p class="drift mono">Last refresh failed — showing what we had. {error}</p>
    {/if}

    {#if board.cards.length === 0}
      <Placeholder
        tone="empty"
        message="Nothing is in flight. When someone requests a film or a show, it appears here while it downloads."
      />
    {:else}
      <ul class="cards">
        {#each board.cards as card, index (`${card.media_type}-${card.created_at}-${index}`)}
          {@const left = formatTimeleft(card.timeleft)}
          {@const count = countLabel(card)}
          <li class="card panel" class:downloading={card.status === 'downloading'}>
            <div class="card-top">
              <span
                class="chip mono"
                class:active={card.status === 'downloading' || card.status === 'partially_available'}
                class:done={card.status === 'available'}
              >
                {statusWord(card)}
              </span>

              {#if card.warning}
                <span class="chip mono warn">{card.warning}</span>
              {/if}

              {#if card.created_at}
                <span class="when mono">{timeAgo(card.created_at)}</span>
              {/if}
            </div>

            <p class="title">{cardTitle(card)}</p>

            {#if card.requested_by}
              <p class="by">for {card.requested_by}</p>
            {/if}

            {#if card.status === 'downloading'}
              <div class="progress">
                <div
                  class="trough"
                  role="progressbar"
                  aria-valuenow={card.pct ?? 0}
                  aria-valuemin="0"
                  aria-valuemax="100"
                  aria-label="Download progress"
                >
                  <span class="fill" style="width: {Math.max(0, Math.min(100, card.pct ?? 0))}%"></span>
                </div>

                <p class="meta mono">
                  <span class="pct">{card.pct ?? 0}%</span>
                  {#if left}<span class="sep">·</span><span>{left} left</span>{/if}
                  {#if count}<span class="sep">·</span><span>{count}</span>{/if}
                </p>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

<style>
  .view {
    animation: rise var(--dur-slow) var(--ease) both;
  }

  .stale,
  .live {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    padding: 2px var(--sp-3);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
    white-space: nowrap;
  }

  .live-dot {
    width: 5px;
    height: 5px;
    border-radius: var(--r-full);
    background: var(--memory);
    box-shadow: var(--glow-memory);
    animation: breathe 3.2s var(--ease) infinite;
  }

  .drift {
    margin-bottom: var(--sp-4);
    color: var(--ember);
    font-size: var(--fs-xs);
  }

  .cards {
    display: flex;
    flex-direction: column;
    gap: var(--sp-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .card {
    padding: var(--sp-4) var(--sp-5);
  }

  /* An active transfer earns a lit left edge; everything else stays stone. */
  .card.downloading {
    border-left-color: rgb(var(--memory-rgb) / 35%);
  }

  .card-top {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    margin-bottom: var(--sp-3);
  }

  .chip {
    padding: 2px var(--sp-2);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .chip.active {
    border-color: rgb(var(--memory-rgb) / 32%);
    background: var(--memory-wash);
    color: var(--memory);
  }

  .chip.done {
    border-color: rgb(131 214 160 / 30%);
    color: var(--moss);
  }

  /* Warm light: this transfer is in trouble. */
  .chip.warn {
    border-color: rgb(var(--rose-rgb) / 40%);
    background: rgb(var(--rose-rgb) / 8%);
    color: var(--ember);
  }

  .when {
    margin-left: auto;
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    white-space: nowrap;
  }

  .title {
    font-size: var(--fs-md);
    text-wrap: balance;
  }

  .by {
    margin-top: 2px;
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .progress {
    margin-top: var(--sp-4);
  }

  .trough {
    overflow: hidden;
    height: 6px;
    border-radius: var(--r-full);
    background: var(--ink-raised);
    box-shadow: inset 0 1px 3px rgb(0 0 0 / 60%);
  }

  .fill {
    display: block;
    height: 100%;
    border-radius: var(--r-full);
    background: linear-gradient(90deg, var(--memory-deep), var(--memory));
    box-shadow: 0 0 12px -1px rgb(var(--memory-rgb) / 60%);
    transition: width var(--dur-slow) var(--ease);
  }

  .meta {
    display: flex;
    gap: var(--sp-2);
    margin-top: var(--sp-2);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
  }

  .pct {
    color: var(--memory);
  }

  .sep {
    opacity: 0.4;
  }
</style>
