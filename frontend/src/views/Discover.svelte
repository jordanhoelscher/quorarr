<script lang="ts">
  /**
   * Everything that isn't on the server yet.
   *
   * Two modes in one view: three browse shelves by default, replaced by a
   * result grid the moment there's a real query. Search is the reason people
   * open this tab — you almost always know what you want — so the field sits
   * above everything and the shelves are what fills the silence.
   *
   * Typing is debounced and versioned: a slow response for "mat" must never
   * overwrite the results for "matrix", which is the classic search bug and
   * looks exactly like the backend returning nonsense.
   */
  import { onMount } from 'svelte';
  import { unreachable } from '../lib/branding.svelte';

  import Placeholder from '../lib/Placeholder.svelte';
  import MediaTile from '../lib/MediaTile.svelte';
  import ViewHead from '../lib/ViewHead.svelte';
  import { ApiError, api } from '../lib/api';
  import type {
    BrowseFilters,
    DiscoverCard,
    DiscoverResults,
    DiscoverShelf,
    DiscoverShelves,
  } from '../lib/types';
  import Browse from './Browse.svelte';
  import DiscoverDetail from './DiscoverDetail.svelte';

  /** Long enough that a fast typist makes one call, short enough to feel live. */
  const DEBOUNCE_MS = 300;
  /** One-letter searches match half of TMDB; not worth the round trip. */
  const MIN_QUERY = 2;

  let shelves = $state<DiscoverShelf[] | null>(null);
  let shelvesError = $state<string | null>(null);

  let query = $state('');
  let submitted = $state('');
  let results = $state<DiscoverCard[] | null>(null);
  let searching = $state(false);
  let searchError = $state<string | null>(null);

  let selected = $state<DiscoverCard | null>(null);

  let browsing = $state<Partial<BrowseFilters> | null>(null);
  /**
   * tmdb_ids requested since this view mounted, passed down to `Browse`.
   * `Browse` owns its own copy of cards (fetched + cached separately, 300s
   * server-side), so a request made from the browse grid has nowhere else to
   * land — this is that landing spot.
   */
  let justRequested = $state<Set<number>>(new Set());

  /**
   * Where each shelf's chevron lands. Trending carries no media type: it is
   * a mixed movie+TV list upstream and accepts no narrowing at all.
   */
  const SHELF_START: Record<string, Partial<BrowseFilters>> = {
    trending: { sort: 'trending' },
    popular: { sort: 'popular', media: 'movie' },
    upcoming: { sort: 'upcoming', media: 'movie' },
  };

  let timer: ReturnType<typeof setTimeout> | undefined;
  /** Bumped per search; a response from an older version is dropped. */
  let version = 0;

  const explain = (err: unknown): string | null => {
    // A 401 has already dropped the whole app to the login screen; showing an
    // error box on the way out would just flash.
    if (err instanceof ApiError && err.status === 401) return null;
    return err instanceof ApiError ? err.message : unreachable();
  };

  const loadShelves = async (): Promise<void> => {
    shelvesError = null;
    try {
      shelves = (await api.get<DiscoverShelves>('/api/discover/shelves')).shelves;
    } catch (err) {
      shelvesError = explain(err);
    }
  };

  onMount(() => {
    void loadShelves();
    return () => clearTimeout(timer);
  });

  const runSearch = async (needle: string): Promise<void> => {
    const mine = (version += 1);
    searching = true;
    searchError = null;
    try {
      const found = await api.get<DiscoverResults>(
        `/api/discover/search?q=${encodeURIComponent(needle)}`,
      );
      if (mine !== version) return;
      results = found.items;
    } catch (err) {
      if (mine !== version) return;
      searchError = explain(err);
      results = null;
    } finally {
      if (mine === version) searching = false;
    }
  };

  const onInput = (event: Event & { currentTarget: HTMLInputElement }): void => {
    query = event.currentTarget.value;
    clearTimeout(timer);

    const needle = query.trim();
    if (needle.length < MIN_QUERY) {
      // Back to the shelves. Bumping the version orphans any in-flight
      // response so a late arrival can't repopulate a grid nobody is looking at.
      version += 1;
      submitted = '';
      results = null;
      searching = false;
      searchError = null;
      return;
    }

    submitted = needle;
    searching = true;
    timer = setTimeout(() => void runSearch(needle), DEBOUNCE_MS);
  };

  const clear = (): void => {
    clearTimeout(timer);
    version += 1;
    query = '';
    submitted = '';
    results = null;
    searching = false;
    searchError = null;
  };

  /**
   * Restate a card as requested once the sheet says the request landed.
   *
   * Cheaper and more honest than a refetch: the browse endpoints are cached
   * for fifteen minutes upstream, so re-reading them would hand back the same
   * "requestable" badge we know is now wrong.
   */
  const markRequested = (tmdbId: number): void => {
    const restate = (card: DiscoverCard): DiscoverCard =>
      card.tmdb_id === tmdbId && card.availability === 'requestable'
        ? { ...card, availability: 'requested' }
        : card;

    if (results) results = results.map(restate);
    if (shelves) shelves = shelves.map((shelf) => ({ ...shelf, items: shelf.items.map(restate) }));
    justRequested = new Set(justRequested).add(tmdbId);
  };
</script>

{#if browsing}
  <Browse
    start={browsing}
    requested={justRequested}
    onback={() => (browsing = null)}
    onselect={(c) => (selected = c)}
  />
{:else}
  <section class="view">
    <ViewHead eyebrow="Not here yet" title="Discover" />

    <div class="finder">
      <svg class="finder-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14ZM16 16l4.5 4.5" />
      </svg>

      <input
        class="finder-input"
        type="search"
        placeholder="Search films and shows"
        aria-label="Search films and shows"
        autocomplete="off"
        value={query}
        oninput={onInput}
      />

      {#if query}
        <button class="finder-clear" aria-label="Clear search" onclick={clear}>×</button>
      {/if}
    </div>

    {#if submitted}
      {#if searching && results === null}
        <Placeholder tone="loading" message="Looking…" />
      {:else if searchError}
        <Placeholder tone="error" message={searchError} onretry={() => void runSearch(submitted)} />
      {:else if results && results.length === 0}
        <Placeholder
          tone="empty"
          message={`Nothing matches “${submitted}”. Try fewer words, or the original title — search reads what TMDB knows, not what's on the server.`}
        />
      {:else if results}
        <p class="tally mono">{results.length} results</p>

        <div class="grid" class:dimmed={searching}>
          {#each results as card, index (`${card.media_type}-${card.tmdb_id}`)}
            <MediaTile {card} {index} onselect={(c) => (selected = c)} />
          {/each}
        </div>
      {/if}
    {:else if shelvesError}
      <Placeholder tone="error" message={shelvesError} onretry={() => void loadShelves()} />
    {:else if shelves === null}
      <Placeholder tone="loading" message="Drawing out what's beyond the basin…" />
    {:else}
      {#each shelves as shelf (shelf.id)}
        <section class="shelf">
          <h3 class="shelf-heading">
            <button class="shelf-title" onclick={() => (browsing = SHELF_START[shelf.id] ?? { sort: 'popular' })}>
              {shelf.title}
              <svg class="shelf-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7" /></svg>
            </button>
          </h3>

          {#if shelf.error}
            <p class="shelf-note">This shelf is unavailable right now.</p>
          {:else if shelf.items.length === 0}
            <p class="shelf-note">Nothing on this shelf today.</p>
          {:else}
            <div class="rail">
              {#each shelf.items as card, index (`${card.media_type}-${card.tmdb_id}`)}
                <MediaTile {card} {index} onselect={(c) => (selected = c)} />
              {/each}
            </div>
          {/if}
        </section>
      {/each}
    {/if}
  </section>
{/if}

{#if selected}
  <DiscoverDetail
    card={selected}
    onclose={() => (selected = null)}
    onrequested={() => selected && markRequested(selected.tmdb_id)}
  />
{/if}

<style>
  .view {
    animation: rise var(--dur-slow) var(--ease) both;
  }

  /* ------------------------------------------------------------ finder */

  .finder {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    margin-bottom: var(--sp-6);
    padding: var(--sp-3) var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    background: var(--ink-raised);
    transition:
      border-color var(--dur-fast) var(--ease),
      box-shadow var(--dur) var(--ease);
  }

  /* The field is the instrument of this view, so it lights when in use. */
  .finder:focus-within {
    border-color: var(--edge-glow);
    box-shadow: var(--glow-memory);
  }

  .finder-icon {
    flex: none;
    width: 18px;
    height: 18px;
    fill: none;
    stroke: var(--rune-dim);
    stroke-width: 1.6;
    stroke-linecap: round;
    transition: stroke var(--dur-fast) var(--ease);
  }

  .finder:focus-within .finder-icon {
    stroke: var(--memory);
  }

  .finder-input {
    flex: 1;
    min-width: 0;
    border: 0;
    background: none;
    color: var(--vapor);
    font: inherit;
    /* 16px floor, or focusing this box zooms the whole app — see --fs-field. */
    font-size: var(--fs-field);
  }

  .finder-input::placeholder {
    color: var(--rune-dim);
  }

  .finder-input:focus {
    outline: none;
  }

  /* The browser's own search affordance duplicates the clear button. */
  .finder-input::-webkit-search-cancel-button {
    display: none;
  }

  .finder-clear {
    flex: none;
    width: 1.5rem;
    height: 1.5rem;
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-md);
    line-height: 1;
    transition: color var(--dur-fast) var(--ease);
  }

  .finder-clear:hover {
    color: var(--vapor);
  }

  /* ------------------------------------------------------------ shelves */

  .shelf {
    margin-bottom: var(--sp-6);
  }

  .shelf-heading {
    margin: 0 0 var(--sp-3);
    font-size: var(--fs-md);
    font-weight: inherit;
  }

  .shelf-title {
    display: flex;
    align-items: center;
    gap: var(--sp-1);
    padding: 0;
    font-size: inherit;
    color: var(--rune);
    transition: color var(--dur-fast) var(--ease);
  }

  .shelf-title:hover {
    color: var(--vapor);
  }

  .shelf-chevron {
    width: 16px;
    height: 16px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.8;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .shelf-note {
    color: var(--rune-dim);
    font-size: var(--fs-sm);
  }

  /*
   * Bleeds to the viewport edges rather than stopping at the shell padding —
   * a rail that ends flush reads as a finished list, and these never are.
   */
  .rail {
    display: flex;
    gap: var(--sp-3);
    margin-inline: calc(-1 * var(--sp-4));
    padding-inline: var(--sp-4);
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scroll-snap-type: x proximity;
    scrollbar-width: none;
  }

  .rail::-webkit-scrollbar {
    display: none;
  }

  .rail :global(.card) {
    flex: none;
    width: 7.5rem;
    scroll-snap-align: start;
  }

  /* ------------------------------------------------------------- cards */

  .tally {
    margin-bottom: var(--sp-3);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(7rem, 1fr));
    gap: var(--sp-4) var(--sp-3);
    transition: opacity var(--dur) var(--ease);
  }

  /* A refined query is loading over the old results, not instead of them. */
  .grid.dimmed {
    opacity: 0.45;
  }
</style>
