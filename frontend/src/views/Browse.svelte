<script lang="ts">
  /**
   * Endless, filtered browsing — the page behind every shelf title.
   *
   * Two things here are less obvious than they look.
   *
   * Requests are versioned. Filter changes are fast and pagination is often
   * in flight, so a slow page-1 response for "Horror" must never land after
   * the user has moved to "Comedy". Discover's search guards the same class
   * of bug; here it bites harder because two request kinds race.
   *
   * A failed page N keeps the rows already on screen and offers a retry at
   * the bottom. Throwing away sixty loaded titles because page four blipped
   * is a worse outcome than the blip.
   */
  import { onMount } from 'svelte';
  import { unreachable } from '../lib/branding.svelte';

  import BrowseFilterBar from '../lib/BrowseFilters.svelte';
  import MediaTile from '../lib/MediaTile.svelte';
  import Placeholder from '../lib/Placeholder.svelte';
  import { ApiError, api } from '../lib/api';
  import { DEFAULT_FILTERS, applyRequested, browseQuery, cardKey, mergeCards, narrow } from '../lib/browse';
  import type { BrowseFilters, BrowseResults, DiscoverCard, Genre } from '../lib/types';

  interface Props {
    /** Where the tapped shelf drops the user in. */
    start: Partial<BrowseFilters>;
    /** tmdb_ids requested elsewhere (e.g. Discover's shelves) since this view mounted. */
    requested: ReadonlySet<number>;
    onback: () => void;
    onselect: (card: DiscoverCard) => void;
  }

  const { start, requested, onback, onselect }: Props = $props();

  // svelte-ignore state_referenced_locally -- deliberate: `start` is only the
  // shelf's drop-in point. `filters` must then diverge freely as the user
  // changes them; making this a $derived would reset the user's own filter
  // choices every time the parent re-renders.
  let filters = $state<BrowseFilters>({ ...DEFAULT_FILTERS, ...start });
  let genres = $state<Genre[]>([]);
  let items = $state<DiscoverCard[]>([]);
  let page = $state(0);
  let hasMore = $state(true);
  let loading = $state(false);
  let error = $state<string | null>(null);
  /**
   * Set when a page load 401'd. `explain()` returns null for a 401 (the app
   * is already swapping to the login screen), which otherwise leaves `error`
   * falsy and `items` empty — indistinguishable from a real zero-result
   * query. Guards the empty-state branch below from flashing "Nothing
   * matches those filters" during that swap.
   */
  let unauthorized = $state(false);

  /** Bumped on every filter change; a response from an older version is dropped. */
  let version = 0;
  let sentinel = $state<HTMLDivElement | null>(null);

  const SORT_TITLES: Record<BrowseFilters['sort'], string> = {
    trending: 'Trending',
    popular: 'Popular',
    newest: 'Newest',
    upcoming: 'Coming soon',
    top_rated: 'Highest rated',
  };

  const explain = (err: unknown): string | null => {
    // A 401 has already dropped the app to the login screen.
    if (err instanceof ApiError && err.status === 401) return null;
    return err instanceof ApiError ? err.message : unreachable();
  };

  const loadGenres = async (media: BrowseFilters['media']): Promise<void> => {
    try {
      genres = (await api.get<{ genres: Genre[] }>(`/api/discover/genres/${media}`)).genres;
    } catch {
      // A missing genre list costs one filter, not the page. Leave it empty.
      genres = [];
    }
  };

  const loadPage = async (next: number): Promise<void> => {
    if (loading) return;
    const mine = version;
    loading = true;
    error = null;
    try {
      const body = await api.get<BrowseResults>(browseQuery(filters, next));
      if (mine !== version) return;
      // mergeCards dedupes against what's already on screen — TMDB repeats
      // titles across adjacent pages, and Svelte 5 throws on a duplicate
      // {#each} key in production. Route page 1 through it too, in case a
      // single page ever repeats itself.
      items = mergeCards(next === 1 ? [] : items, body.items);
      page = body.page;
      hasMore = body.has_more;
    } catch (err) {
      if (mine !== version) return;
      if (err instanceof ApiError && err.status === 401) unauthorized = true;
      error = explain(err);
      // Stop the observer hammering a failing endpoint; retry re-arms it.
      hasMore = false;
    } finally {
      if (mine === version) loading = false;
    }
  };

  const restart = (): void => {
    version += 1;
    items = [];
    page = 0;
    hasMore = true;
    error = null;
    unauthorized = false;
    loading = false;
    void loadPage(1);
  };

  const change = (patch: Partial<BrowseFilters>): void => {
    const next = narrow(filters, patch);
    if (JSON.stringify(next) === JSON.stringify(filters)) return;
    const mediaChanged = next.media !== filters.media;
    filters = next;
    if (mediaChanged) void loadGenres(next.media);
    restart();
  };

  const retry = (): void => {
    hasMore = true;
    void loadPage(page + 1);
  };

  /** The empty state's way out: drop what the user picked, keep sort/media. */
  const clearFilters = (): void => change({ genre: null, decade: null, minRating: null });

  const canClearFilters = $derived(
    filters.genre !== null || filters.decade !== null || filters.minRating !== null,
  );

  /** Requests made from Discover's shelves/search reflected onto this grid. */
  const rendered = $derived(applyRequested(items, requested));

  onMount(() => {
    void loadGenres(filters.media);
    restart();
  });

  /**
   * Top level, not inside onMount: `$effect` must be created during component
   * initialisation, and onMount's callback runs after that window has closed.
   *
   * Re-runs when `sentinel` binds, and tears the observer down each time, so
   * the node is never observed twice. `rootMargin` starts the next fetch
   * before the user reaches the bottom, which is what makes it feel endless
   * rather than stop-start.
   *
   * Also re-runs on `items.length`: IntersectionObserver only reports
   * *crossings* of the root margin, not the sentinel's current state. If the
   * sentinel is still inside `rootMargin` after a page renders (a wide
   * viewport can fit 20 tiles in two rows, well within 600px), no crossing
   * ever happens and no further callback fires — the view silently stalls
   * at page 1. Recreating the observer after every append forces a fresh
   * initial callback while the sentinel is still in view, which re-arms
   * loading; the `page > 0` guard still rejects the very first mount-time
   * callback, and `hasMore` still stops it at the end.
   */
  $effect(() => {
    const node = sentinel;
    void items.length;
    if (!node) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && hasMore && !loading && page > 0) {
          void loadPage(page + 1);
        }
      },
      { rootMargin: '600px' },
    );
    observer.observe(node);

    return () => observer.disconnect();
  });
</script>

<section class="view">
  <header class="head">
    <button class="back" onclick={onback}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7" /></svg>
      Discover
    </button>
    <h2>{SORT_TITLES[filters.sort]}</h2>
  </header>

  <BrowseFilterBar {filters} {genres} onchange={change} />

  {#if rendered.length > 0}
    <div class="grid">
      {#each rendered as card, index (cardKey(card))}
        <MediaTile {card} index={index % 12} {onselect} />
      {/each}
    </div>
  {/if}

  {#if error && items.length === 0}
    <Placeholder tone="error" message={error} onretry={restart} />
  {:else if loading && items.length === 0}
    <Placeholder tone="loading" message="Reaching further into the basin…" />
  {:else if !loading && items.length === 0 && !unauthorized}
    <Placeholder tone="empty" message="Nothing matches those filters." />
    {#if canClearFilters}
      <p class="foot">
        <button class="link" onclick={clearFilters}>Clear filters</button>
      </p>
    {/if}
  {/if}

  {#if error && items.length > 0}
    <p class="foot">
      Couldn't load more. <button class="link" onclick={retry}>Try again</button>
    </p>
  {:else if loading && items.length > 0}
    <p class="foot mono">Loading…</p>
  {:else if !hasMore && items.length > 0}
    <p class="foot mono">That's everything.</p>
  {/if}

  <div bind:this={sentinel} class="sentinel" aria-hidden="true"></div>
</section>

<style>
  .head {
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    margin-bottom: var(--sp-5);
  }

  .back {
    display: flex;
    align-items: center;
    gap: var(--sp-1);
    align-self: flex-start;
    padding: 0;
    color: var(--rune-dim);
    font-size: var(--fs-sm);
    transition: color var(--dur-fast) var(--ease);
  }

  .back:hover {
    color: var(--vapor);
  }

  .back svg {
    width: 18px;
    height: 18px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  h2 {
    margin: 0;
    font-family: var(--font-display);
    font-size: var(--fs-xl);
    letter-spacing: -0.02em;
    color: var(--vapor);
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(7rem, 1fr));
    gap: var(--sp-4) var(--sp-3);
  }

  .foot {
    margin-top: var(--sp-5);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
    text-align: center;
  }

  .link {
    padding: 0;
    color: var(--memory);
    font-size: inherit;
    text-decoration: underline;
  }

  .sentinel {
    height: 1px;
  }
</style>
