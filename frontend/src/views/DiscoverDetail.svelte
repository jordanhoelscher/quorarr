<script lang="ts">
  /**
   * One title, and the only place a friend adds something to the server.
   *
   * Opens instantly on the card the shelf already has — poster, title, year —
   * then fills in overview and (for TV) per-season availability from the
   * detail endpoint. Until that lands there is no Request button: asking for
   * a show without knowing which seasons are already on the server is exactly
   * the duplicate this feature exists to prevent.
   *
   * The house bottom-sheet idiom, same as `ActionSheet`: the parent renders
   * this only while a card is selected and clears the card in `onclose`, so
   * every open is a fresh instance with no state to reset.
   */
  import { onMount } from 'svelte';
  import { SENT_FOR_APPROVAL, branding, unreachable } from '../lib/branding.svelte';

  import Poster from '../lib/Poster.svelte';
  import { ApiError, api } from '../lib/api';
  import { posterUrl } from '../lib/format';
  import { session } from '../lib/session.svelte';
  import { toasts } from '../lib/toast.svelte';
  import type {
    Availability,
    DiscoverCard,
    DiscoverDetailCard,
    DiscoverQuality,
    RequestResult,
  } from '../lib/types';

  interface Props {
    /** The shelf/search card — header content while the detail fetch is in flight. */
    card: DiscoverCard;
    onclose: () => void;
    /** Called after a request lands, so the grid can restate availability. */
    onrequested?: () => void;
  }

  const { card, onclose, onrequested }: Props = $props();

  // Swipe-down-to-dismiss: the handle bar advertises it, so honor it.
  let dragY = $state(0);
  let dragging = $state(false);
  let startY = 0;
  let sheetEl: HTMLDivElement | undefined = $state();

  // Lock the page behind the sheet while it's open -- otherwise drag
  // gestures scroll-chain into the tab content underneath.
  $effect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  });

  const dragStart = (e: TouchEvent): void => {
    if (busy) return;
    // Only begin a dismiss-drag when the sheet's own scroll is at the top,
    // so normal content scrolling keeps working.
    if (sheetEl && sheetEl.scrollTop > 0) return;
    startY = e.touches[0].clientY;
    dragging = true;
  };

  const dragMove = (e: TouchEvent): void => {
    if (!dragging) return;
    const dy = e.touches[0].clientY - startY;
    if (dy > 0) {
      dragY = dy;
      e.preventDefault();
    }
  };

  const dragEnd = (): void => {
    if (!dragging) return;
    dragging = false;
    if (dragY > 110) onclose();
    dragY = 0;
  };

  let detail = $state<DiscoverDetailCard | null>(null);
  let failed = $state<string | null>(null);
  let busy = $state(false);
  /** Season numbers the picker has ticked. Seeded once, from the detail fetch. */
  let picked = $state<Set<number>>(new Set());
  /** Which lane to file against. 1080p is the default everywhere. */
  let quality = $state<DiscoverQuality>('1080p');

  const BADGE_WORDS: Record<Availability, string | null> = {
    available: 'Available',
    partial: 'Partly there',
    requested: 'Requested',
    requestable: null,
  };

  onMount(async () => {
    try {
      const loaded = await api.get<DiscoverDetailCard>(
        `/api/discover/detail/${card.media_type}/${card.tmdb_id}`,
      );
      // Everything still askable starts ticked: the common ask is "all of it",
      // and un-ticking three seasons is less work than ticking eight.
      picked = new Set((loaded.seasons ?? []).filter((s) => s.requestable).map((s) => s.season_number));
      detail = loaded;
    } catch (err) {
      // A 401 has already dropped the whole app to the login screen.
      if (!(err instanceof ApiError && err.status === 401)) {
        failed = err instanceof ApiError ? err.message : unreachable();
      }
    }
  });

  const shown = $derived(detail ?? card);
  const isTv = $derived(card.media_type === 'tv');
  const seasons = $derived(detail?.seasons ?? []);
  const openSeasons = $derived(seasons.filter((season) => season.requestable));
  const canRequest = $derived(
    detail !== null && (isTv ? picked.size > 0 : detail.availability === 'requestable'),
  );

  const isOwner = $derived(session.user?.role === 'owner');

  /**
   * The lanes on offer, cheapest first.
   *
   * 720p is TV-only: it exists for sitcoms and background TV, and Radarr has
   * no equivalent lane, so the server 422s it on a film rather than pretend.
   * 4K is offered to everyone, but it means two different things — the owner's
   * files immediately, a friend's becomes a request awaiting sign-off. The
   * note says which, because a chip that quietly behaves differently
   * depending on who is looking at it is a trap.
   */
  const lanes = $derived([
    ...(isTv
      ? [{ value: '720p' as const, note: 'Space-saver — sitcoms, background TV' }]
      : []),
    { value: '1080p' as const, note: 'The good stuff' },
    {
      value: '4K' as const,
      note: isOwner ? 'Straight through' : `Needs ${branding.ownerName}'s sign-off`,
    },
  ]);

  const toggle = (seasonNumber: number): void => {
    // Reassign rather than mutate — a Set mutated in place is not a state change.
    const next = new Set(picked);
    if (!next.delete(seasonNumber)) next.add(seasonNumber);
    picked = next;
  };

  const seasonLabel = (season: { season_number: number; name: string }): string =>
    season.name || `Season ${season.season_number}`;

  const submit = async (): Promise<void> => {
    if (busy || !canRequest) return;
    busy = true;
    try {
      const result = await api.post<RequestResult>('/api/discover/request', {
        media_type: card.media_type,
        tmdb_id: card.tmdb_id,
        seasons: isTv ? [...picked].sort((a, b) => a - b) : null,
        quality,
      });
      // A friend's 4K files nothing upstream — saying "on the board" would
      // have them watching Pipeline for something that isn't coming yet.
      toasts.push(
        result.state === 'pending_approval'
          ? SENT_FOR_APPROVAL()
          : 'On the board — watch Pipeline',
      );
      onrequested?.();
      onclose();
    } catch (err) {
      // The server's own words: a duplicate refusal comes from Jellyseerr,
      // and "you already asked for this" beats anything invented here.
      toasts.push(
        err instanceof ApiError ? err.message : unreachable(),
        'warn',
      );
      busy = false;
    }
  };

  const requestWord = $derived.by((): string => {
    if (busy) return 'Sending…';
    const ask = quality === '4K' && !isOwner ? 'Ask for' : 'Request';
    if (!isTv) return `${ask} this film`;
    if (picked.size === 0) return 'Pick a season';
    return picked.size === 1 ? `${ask} 1 season` : `${ask} ${picked.size} seasons`;
  });
</script>

<svelte:window onkeydown={(event) => event.key === 'Escape' && !busy && onclose()} />

<div class="layer">
  <button class="scrim" aria-label="Close" onclick={onclose}></button>

  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="discover-sheet-title"
    tabindex="-1"
    bind:this={sheetEl}
    style:transform={dragY ? `translateY(${dragY}px)` : ""}
    style:transition={dragging ? "none" : ""}
    ontouchstart={dragStart} ontouchmove={dragMove} ontouchend={dragEnd} ontouchcancel={dragEnd}>
    <span class="grip" aria-hidden="true"></span>

    <header class="head">
      <Poster src={posterUrl(shown.poster_path, 'w342')} size="lead" />

      <div class="ident">
        <h3 id="discover-sheet-title">{shown.title}</h3>

        <p class="facts mono">
          {#if shown.year}<span>{shown.year}</span><span class="sep" aria-hidden="true">·</span>{/if}
          <span>{isTv ? 'TV' : 'Film'}</span>
          {#if shown.rating}
            <span class="sep" aria-hidden="true">·</span><span>{shown.rating.toFixed(1)}</span>
          {/if}
        </p>

        {#if BADGE_WORDS[shown.availability]}
          <span class="badge" class:held={shown.availability !== 'available'}>
            {BADGE_WORDS[shown.availability]}
          </span>
        {/if}
      </div>
    </header>

    {#if failed}
      <p class="notice">{failed}</p>
    {:else if detail === null}
      <p class="notice loading"><span class="pulse" aria-hidden="true"></span>Reading the record…</p>
    {:else}
      {#if detail.overview}
        <p class="overview">{detail.overview}</p>
      {/if}

      {#if isTv}
        {#if seasons.length === 0}
          <p class="notice">No seasons listed for this one yet.</p>
        {:else}
          <fieldset class="seasons">
            <legend class="eyebrow">Seasons</legend>

            {#each seasons as season (season.season_number)}
              <label class="season" class:off={!season.requestable}>
                <input
                  type="checkbox"
                  checked={picked.has(season.season_number)}
                  disabled={!season.requestable || busy}
                  onchange={() => toggle(season.season_number)}
                />
                <span class="tick" aria-hidden="true"></span>

                <span class="season-name">{seasonLabel(season)}</span>

                <span class="season-note mono">
                  {BADGE_WORDS[season.availability] ??
                    (season.episode_count ? `${season.episode_count} eps` : '')}
                </span>
              </label>
            {/each}
          </fieldset>

          {#if openSeasons.length === 0}
            <p class="notice">Every season is already on the server or already requested.</p>
          {/if}
        {/if}
      {:else if detail.availability !== 'requestable'}
        <p class="notice">
          {detail.availability === 'available'
            ? 'Already available — find it in Plex.'
            : 'Already asked for. Watch it move on the Pipeline board.'}
        </p>
      {/if}

      {#if canRequest}
        <fieldset class="lanes">
          <legend class="eyebrow">Quality</legend>

          <div class="chips">
            {#each lanes as lane (lane.value)}
              <button
                type="button"
                class="chip"
                class:on={quality === lane.value}
                aria-pressed={quality === lane.value}
                disabled={busy}
                onclick={() => (quality = lane.value)}
              >
                <span class="chip-name mono">{lane.value}</span>
                <span class="chip-note">{lane.note}</span>
              </button>
            {/each}
          </div>
        </fieldset>
      {/if}

      <div class="foot">
        <button class="back" disabled={busy} onclick={onclose}>Close</button>
        {#if canRequest}
          <button class="request" disabled={busy} onclick={submit}>{requestWord}</button>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .layer {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
  }

  .scrim {
    position: absolute;
    inset: 0;
    background: rgb(3 5 8 / 68%);
    backdrop-filter: blur(3px);
    -webkit-backdrop-filter: blur(3px);
    cursor: default;
    animation: fade var(--dur) var(--ease) both;
  }

  @keyframes fade {
    from {
      opacity: 0;
    }
  }

  .sheet {
    overscroll-behavior: contain;
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
    width: min(32rem, 100%);
    max-height: 92dvh;
    overflow-y: auto;
    overscroll-behavior: contain;
    margin-inline: auto;
    padding: var(--sp-3) var(--sp-5) calc(var(--sp-6) + env(safe-area-inset-bottom));
    border: 1px solid var(--edge-hi);
    border-bottom: 0;
    border-radius: var(--r-lg) var(--r-lg) 0 0;
    background:
      linear-gradient(180deg, rgb(255 255 255 / 4%), transparent 30%),
      var(--basin);
    box-shadow: var(--shadow-lift);
    animation: slide-up var(--dur) var(--ease) both;
  }

  @keyframes slide-up {
    from {
      opacity: 0;
      transform: translateY(28px);
    }
  }

  .grip {
    flex: none;
    align-self: center;
    width: 2.25rem;
    height: 3px;
    border-radius: var(--r-full);
    background: var(--edge-hi);
  }

  /* ------------------------------------------------------------- head */

  .head {
    display: flex;
    gap: var(--sp-4);
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
    flex-wrap: wrap;
    gap: var(--sp-2);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .sep {
    opacity: 0.4;
  }

  .badge {
    padding: 2px var(--sp-3);
    border: 1px solid rgb(var(--memory-rgb) / 30%);
    border-radius: var(--r-full);
    background: var(--memory-wash);
    color: var(--memory);
    font-family: var(--font-mono);
    font-size: var(--fs-micro);
    letter-spacing: 0.08em;
  }

  /* In flight, not landed: the same cold light, turned down. */
  .badge.held {
    border-color: var(--edge-hi);
    background: none;
    color: var(--rune);
  }

  /* ------------------------------------------------------------- body */

  .overview {
    color: var(--rune);
    font-size: var(--fs-sm);
    text-wrap: pretty;
  }

  .notice {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    color: var(--rune-dim);
    font-size: var(--fs-sm);
    text-wrap: pretty;
  }

  .pulse {
    flex: none;
    width: 7px;
    height: 7px;
    border-radius: var(--r-full);
    background: var(--memory);
    box-shadow: var(--glow-memory);
    animation: breathe 2.4s var(--ease) infinite;
  }

  /* ---------------------------------------------------------- seasons */

  .seasons {
    display: flex;
    flex-direction: column;
    margin: 0;
    padding: 0;
    border: 0;
    border-top: 1px solid var(--edge);
  }

  .seasons legend {
    padding: 0 0 var(--sp-2);
  }

  .season {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    padding: var(--sp-3) var(--sp-1);
    border-bottom: 1px solid var(--edge);
    cursor: pointer;
    transition: background-color var(--dur-fast) var(--ease);
  }

  .season:hover:not(.off) {
    background: rgb(var(--memory-rgb) / 4%);
  }

  /* Recessed, not hidden: knowing a season is already here is the point. */
  .season.off {
    cursor: default;
    opacity: 0.5;
  }

  .season input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
    pointer-events: none;
  }

  /* An empty socket that fills with cold light when the season is picked. */
  .seasons {
    /* fieldset defaults to min-inline-size: min-content, overflowing the
       sheet and clipping the right-hand column. */
    min-inline-size: 0;
    border: 0;
    padding: 0;
    margin: 0;
  }

  .season-note {
    flex: none;
  }

  .tick {
    position: relative;
    flex: none;
    width: 1.15rem;
    height: 1.15rem;
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-sm);
    background: var(--ink-raised);
    transition:
      border-color var(--dur-fast) var(--ease),
      background-color var(--dur-fast) var(--ease);
  }

  .season input:checked + .tick {
    border-color: transparent;
    background: var(--memory);
    box-shadow: var(--glow-memory);
  }

  .season input:checked + .tick::after {
    /* Proper checkmark proportions: a tall-narrow box, right+bottom borders,
       rotated 45deg -- the old squat inset read as a down-chevron. */
    content: '';
    position: absolute;
    left: 0.36rem;
    top: 0.12rem;
    width: 0.3rem;
    height: 0.6rem;
    border: solid var(--ink);
    border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }

  .season input:focus-visible + .tick {
    outline: 2px solid var(--memory);
    outline-offset: 3px;
  }

  .season-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--vapor);
    font-size: var(--fs-sm);
  }

  .season-note {
    flex: none;
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
  }

  /* ------------------------------------------------------------ lanes */

  .lanes {
    /* Same fieldset defence as .seasons: min-content would overflow the sheet. */
    min-inline-size: 0;
    margin: 0;
    padding: 0;
    border: 0;
    border-top: 1px solid var(--edge);
  }

  .lanes legend {
    padding: 0 0 var(--sp-2);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-2);
    padding-top: var(--sp-2);
  }

  .chip {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    flex: 1 1 8rem;
    padding: var(--sp-2) var(--sp-3);
    border: 1px solid var(--edge);
    border-radius: var(--r-md);
    background: var(--ink-raised);
    text-align: left;
    transition:
      border-color var(--dur-fast) var(--ease),
      background-color var(--dur-fast) var(--ease);
  }

  .chip:hover:not(:disabled):not(.on) {
    border-color: var(--edge-hi);
  }

  /* The picked lane wears the same cold light as a ticked season. */
  .chip.on {
    border-color: rgb(var(--memory-rgb) / 45%);
    background: var(--memory-wash);
  }

  .chip-name {
    color: var(--rune);
    font-size: var(--fs-xs);
    letter-spacing: 0.06em;
  }

  .chip.on .chip-name {
    color: var(--memory);
  }

  .chip-note {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    text-wrap: pretty;
  }

  .chip:focus-visible {
    outline: 2px solid var(--memory);
    outline-offset: 2px;
  }

  /* ------------------------------------------------------------- foot */

  .foot {
    display: flex;
    gap: var(--sp-3);
    margin-top: var(--sp-1);
  }

  .back {
    padding: var(--sp-3) var(--sp-5);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-sm);
  }

  .back:hover:not(:disabled) {
    color: var(--vapor);
    border-color: var(--edge-hi);
  }

  .request {
    flex: 1;
    padding: var(--sp-3) var(--sp-4);
    border: 1px solid rgb(var(--memory-rgb) / 45%);
    border-radius: var(--r-full);
    background: var(--memory-wash);
    color: var(--memory);
    font-size: var(--fs-sm);
    transition: background-color var(--dur-fast) var(--ease);
  }

  .request:hover:not(:disabled) {
    background: rgb(var(--memory-rgb) / 18%);
  }

  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
</style>
