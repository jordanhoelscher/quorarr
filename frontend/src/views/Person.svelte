<script lang="ts">
  /**
   * One actor, and everything they have acted in that TMDB knows about.
   *
   * The same poster grid as the browse drill-down, and deliberately so: the
   * cards are ordinary Discover cards, so every tile opens the detail sheet
   * and files through the same guarded request lane. Nothing here is a second
   * way to ask for something.
   *
   * Unlike `Browse` this loads once and does not paginate — the backend caps
   * the list at fifty by popularity, which is the part of a prolific
   * filmography anyone actually scrolls.
   */
  import { onMount } from 'svelte';
  import { unreachable } from '../lib/branding.svelte';

  import MediaTile from '../lib/MediaTile.svelte';
  import Placeholder from '../lib/Placeholder.svelte';
  import Poster from '../lib/Poster.svelte';
  import { ApiError, api } from '../lib/api';
  import { applyRequested, cardKey } from '../lib/browse';
  import { posterUrl } from '../lib/format';
  import type { DiscoverCard, PersonFilmography } from '../lib/types';

  interface Props {
    personId: number;
    /**
     * The name from the suggestion row that was tapped. Renders the header
     * before the fetch answers, so opening an actor does not flash a blank
     * title; the response replaces it with the authoritative one.
     */
    seedName: string;
    /** tmdb_ids requested elsewhere since Discover mounted. */
    requested: ReadonlySet<number>;
    onback: () => void;
    onselect: (card: DiscoverCard) => void;
  }

  const { personId, seedName, requested, onback, onselect }: Props = $props();

  let person = $state<PersonFilmography | null>(null);
  let error = $state<string | null>(null);
  /**
   * Set when the load 401'd. `explain()` answers null for a 401 (the app is
   * already swapping to the login screen), which would otherwise leave both
   * `error` and `person` empty — indistinguishable from an actor with no
   * credits.
   */
  let unauthorized = $state(false);

  const name = $derived(person?.name || seedName);
  const rendered = $derived(applyRequested(person?.items ?? [], requested));

  const load = async (): Promise<void> => {
    error = null;
    unauthorized = false;
    try {
      person = await api.get<PersonFilmography>(`/api/discover/person/${personId}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) unauthorized = true;
      error = err instanceof ApiError ? (err.status === 401 ? null : err.message) : unreachable();
    }
  };

  onMount(() => void load());
</script>

<section class="view">
  <header class="head">
    <button class="back" onclick={onback}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7" /></svg>
      Discover
    </button>

    <div class="who">
      <Poster src={posterUrl(person?.profile_path ?? null, 'w185')} size="face" alt="" />
      <div>
        <h2>{name}</h2>
        <p class="eyebrow mono">Acting credits</p>
      </div>
    </div>
  </header>

  {#if rendered.length > 0}
    <div class="grid">
      {#each rendered as card, index (cardKey(card))}
        <MediaTile {card} index={index % 12} {onselect} />
      {/each}
    </div>
  {:else if error}
    <Placeholder tone="error" message={error} onretry={() => void load()} />
  {:else if person === null && !unauthorized}
    <Placeholder tone="loading" message="Tracing what they have been in…" />
  {:else if person !== null}
    <Placeholder tone="empty" message={`No acting credits for ${name}.`} />
  {/if}
</section>

<style>
  .head {
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
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

  .who {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
  }

  h2 {
    margin: 0;
    font-family: var(--font-display);
    font-size: var(--fs-xl);
    letter-spacing: -0.02em;
    line-height: 1.1;
    color: var(--vapor);
  }

  .eyebrow {
    margin: var(--sp-1) 0 0;
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(7rem, 1fr));
    gap: var(--sp-4) var(--sp-3);
  }
</style>
