<script lang="ts">
  /**
   * Everything between "someone asked for it" and "it is watchable".
   *
   * A staged wall of poster art: one section per stage that has anything in
   * it, three tiles across, status carried by the artwork itself — dimmed is
   * waiting, lit with a bar is moving, a check means watchable now. The
   * grouping rules live in `lib/pipelineStages`, not here.
   *
   * Refreshes itself every 30s, but only while the tab is actually visible —
   * a phone in a pocket should not be polling. A background refresh never
   * flips the view back to its loading state: the numbers move, the layout
   * does not, so watching a download tick along doesn't strobe.
   */
  import { onMount } from 'svelte';
  import { unreachable } from '../lib/branding.svelte';

  import DiscoverDetail from './DiscoverDetail.svelte';
  import Placeholder from '../lib/Placeholder.svelte';
  import Poster from '../lib/Poster.svelte';
  import ViewHead from '../lib/ViewHead.svelte';
  import { ApiError, api } from '../lib/api';
  import { formatTimeleft, posterUrl, staleLabel, timeAgo } from '../lib/format';
  import { groupByStage } from '../lib/pipelineStages';
  import type { DiscoverCard, PipelineBoard, PipelineCard } from '../lib/types';

  const REFRESH_MS = 30_000;

  let board = $state<PipelineBoard | null>(null);
  let error = $state<string | null>(null);
  let loading = $state(true);
  let selected = $state<DiscoverCard | null>(null);

  const stages = $derived(board ? groupByStage(board.cards) : []);

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

  const countLabel = (card: PipelineCard): string | null => {
    if (!card.count) return null;
    if (card.media_type === 'movie') return card.count > 1 ? `${card.count} files` : '1 file';
    return card.count > 1 ? `${card.count} episodes` : '1 episode';
  };

  /**
   * The line under the title: whatever is actually changing about this card.
   *
   * The stage header already says what state the card is in, so repeating it
   * here would spend a line on nothing. What varies *within* a stage is how
   * long it has left, how many episodes it covers, or how long it has been
   * sitting — and a stall, which is the one thing worth reading first.
   */
  const statusLine = (card: PipelineCard): string | null => {
    if (card.warning) return card.warning;

    if (card.status === 'downloading') {
      const parts = [formatTimeleft(card.timeleft), countLabel(card)].filter(Boolean);
      return parts.length ? parts.join(' · ') : `${card.pct ?? 0}%`;
    }

    return card.created_at ? timeAgo(card.created_at) : null;
  };

  /**
   * Synthesise the Discover card the detail sheet opens on.
   *
   * `availability` is pinned to what the pipeline already knows rather than
   * guessed: every card on this board has been requested, so the sheet must
   * never offer a Request button for something already in flight.
   */
  const AVAILABILITY = {
    available: 'available',
    partially_available: 'partial',
  } as const;

  const openDetail = (card: PipelineCard): void => {
    if (card.tmdb_id === null) return;
    selected = {
      tmdb_id: card.tmdb_id,
      title: cardTitle(card),
      year: null,
      media_type: card.media_type === 'tv' ? 'tv' : 'movie',
      poster_path: card.poster,
      overview: '',
      rating: null,
      status: null,
      availability:
        AVAILABILITY[card.status as keyof typeof AVAILABILITY] ?? 'requested',
    };
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
      {#each stages as stage (stage.key)}
        <section class="stage">
          <h3 class="stage-head">
            <span class="eyebrow" class:on={stage.key === 'downloading'}>{stage.label}</span>
            <span class="rule" aria-hidden="true"></span>
            <span class="count mono">{stage.cards.length}</span>
          </h3>

          <ul class="tiles">
            {#each stage.cards as card, index (`${card.media_type}-${card.tmdb_id}-${card.created_at}-${index}`)}
              {@const status = statusLine(card)}
              {@const title = cardTitle(card)}
              <li>
                <button
                  class="tile"
                  disabled={card.tmdb_id === null}
                  onclick={() => openDetail(card)}
                >
                  <span
                    class="art"
                    class:on={card.status === 'downloading'}
                    class:done={card.status === 'available'}
                    class:wait={card.status !== 'downloading' && card.status !== 'available'}
                  >
                    <Poster src={posterUrl(card.poster)} size="tile" />

                    {#if card.status === 'downloading'}
                      <span class="veil">
                        <span class="pct mono">{card.pct ?? 0}%</span>
                        <span
                          class="trough"
                          role="progressbar"
                          aria-valuenow={card.pct ?? 0}
                          aria-valuemin="0"
                          aria-valuemax="100"
                          aria-label="Download progress for {title}"
                        >
                          <span
                            class="fill"
                            style="width: {Math.max(0, Math.min(100, card.pct ?? 0))}%"
                          ></span>
                        </span>
                      </span>
                    {:else if card.warning}
                      <span class="badge bad mono" aria-hidden="true">!</span>
                    {:else if card.status === 'available'}
                      <span class="badge ok" aria-hidden="true">✓</span>
                    {/if}
                  </span>

                  <span class="name">{title}</span>
                  {#if status}
                    <span
                      class="meta mono"
                      class:on={card.status === 'downloading'}
                      class:bad={card.warning}
                    >
                      {status}
                    </span>
                  {/if}
                  {#if card.requested_by}
                    <span class="who mono">{card.requested_by}</span>
                  {/if}
                </button>
              </li>
            {/each}
          </ul>
        </section>
      {/each}
    {/if}
  {/if}
</section>

{#if selected}
  <DiscoverDetail card={selected} onclose={() => (selected = null)} />
{/if}

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

  /* ------------------------------------------------------------- stages */

  .stage {
    margin-bottom: var(--sp-5);
  }

  .stage-head {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    margin-bottom: var(--sp-3);
    font-family: var(--font-ui);
    letter-spacing: normal;
  }

  /* The one lit label: something is actually transferring right now. */
  .eyebrow.on {
    color: var(--memory);
  }

  .rule {
    flex: 1;
    height: 1px;
    background: linear-gradient(90deg, var(--edge-hi), transparent);
  }

  .count {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
  }

  .tiles {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: var(--sp-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* -------------------------------------------------------------- tiles */

  .tile {
    display: flex;
    flex-direction: column;
    gap: 2px;
    width: 100%;
    padding: 0;
    text-align: left;
  }

  .tile:disabled {
    cursor: default;
  }

  .art {
    position: relative;
    display: block;
    margin-bottom: var(--sp-2);
    border-radius: var(--r-md);
    transition: transform var(--dur) var(--ease);
  }

  .tile:not(:disabled):hover .art,
  .tile:focus-visible .art {
    transform: translateY(-3px);
  }

  /*
   * Status lives in the artwork. Reaching into `Poster` needs `:global` --
   * the img is that component's, not this one's.
   */
  .art.wait :global(.poster) {
    opacity: 0.44;
    filter: saturate(0.42);
  }

  .art.done :global(.poster) {
    opacity: 0.72;
  }

  .art.on {
    box-shadow:
      0 0 0 1px rgb(var(--memory-rgb) / 38%),
      0 0 20px -8px rgb(var(--memory-rgb) / 60%);
  }

  .veil {
    position: absolute;
    inset: auto 0 0;
    padding: 20px var(--sp-2) 5px;
    border-radius: 0 0 var(--r-md) var(--r-md);
    background: linear-gradient(transparent, rgb(3 6 10 / 93%));
  }

  .pct {
    display: block;
    margin-bottom: 3px;
    color: var(--memory);
    font-size: var(--fs-micro);
  }

  .trough {
    display: block;
    overflow: hidden;
    height: 3px;
    border-radius: var(--r-full);
    background: rgb(0 0 0 / 55%);
  }

  .fill {
    display: block;
    height: 100%;
    border-radius: var(--r-full);
    background: linear-gradient(90deg, var(--memory-deep), var(--memory));
    box-shadow: 0 0 10px -1px rgb(var(--memory-rgb) / 70%);
    transition: width var(--dur-slow) var(--ease);
  }

  .badge {
    position: absolute;
    top: 5px;
    right: 5px;
    display: grid;
    place-items: center;
    width: 18px;
    height: 18px;
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-full);
    background: rgb(3 6 10 / 80%);
    font-size: 10px;
  }

  .badge.ok {
    border-color: rgb(131 214 160 / 40%);
    color: var(--moss);
  }

  /* Warm light: this transfer is in trouble. */
  .badge.bad {
    border-color: rgb(var(--rose-rgb) / 50%);
    background: rgb(var(--rose-rgb) / 14%);
    color: var(--ember);
  }

  .name {
    overflow: hidden;
    color: var(--vapor);
    font-size: var(--fs-micro);
    line-height: 1.3;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta,
  .who {
    overflow: hidden;
    color: var(--rune-dim);
    font-size: 10px;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta.on {
    color: var(--memory);
  }

  .meta.bad {
    color: var(--ember);
  }
</style>
