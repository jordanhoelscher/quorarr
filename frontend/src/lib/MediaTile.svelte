<script lang="ts">
  /**
   * One poster tile: art, availability badge, title, kind and year.
   *
   * Shared by the Discover shelf rails, the search grid and the browse grid
   * so those three surfaces cannot drift apart — the badge vocabulary in
   * particular is the sort of thing that gets updated in one place and
   * forgotten in the others.
   *
   * The caller positions it. `--i` staggers the entrance animation; it is
   * clamped by the caller so a grid of 200 does not animate for ten seconds.
   */
  import Poster from './Poster.svelte';
  import { posterUrl } from './format';
  import type { Availability, DiscoverCard } from './types';

  interface Props {
    card: DiscoverCard;
    /** Position in its row/grid, for the staggered entrance. */
    index: number;
    onselect: (card: DiscoverCard) => void;
  }

  const { card, index, onselect }: Props = $props();

  const BADGE_WORDS: Record<Availability, string | null> = {
    available: 'Available',
    partial: 'Partly there',
    requested: 'Requested',
    requestable: null,
  };
</script>

<button class="card" style="--i: {Math.min(index, 11)}" onclick={() => onselect(card)}>
  <span class="art">
    <Poster src={posterUrl(card.poster_path)} size="tile" />
    {#if BADGE_WORDS[card.availability]}
      <span class="badge mono" class:held={card.availability !== 'available'}>
        {BADGE_WORDS[card.availability]}
      </span>
    {/if}
  </span>

  <span class="card-title">{card.title}</span>
  <span class="card-meta mono">
    {card.media_type === 'tv' ? 'TV' : 'Film'}{card.year ? ` · ${card.year}` : ''}
  </span>
</button>

<style>
  .card {
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    padding: 0;
    text-align: left;
    animation: rise var(--dur) var(--ease) both;
    animation-delay: calc(var(--i, 0) * 45ms);
  }

  .art {
    position: relative;
    display: block;
    transition: transform var(--dur) var(--ease);
  }

  .card:hover .art,
  .card:focus-visible .art {
    transform: translateY(-3px);
  }

  .badge {
    position: absolute;
    inset: auto var(--sp-1) var(--sp-1) var(--sp-1);
    padding: 3px var(--sp-2);
    border-radius: var(--r-sm);
    background: rgb(9 14 21 / 88%);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    box-shadow: inset 0 0 0 1px rgb(var(--memory-rgb) / 30%);
    color: var(--memory);
    font-size: var(--fs-micro);
    letter-spacing: 0.04em;
    text-align: center;
  }

  .badge.held {
    box-shadow: inset 0 0 0 1px var(--edge-hi);
    color: var(--rune);
  }

  .card-title {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
    color: var(--vapor);
    font-size: var(--fs-sm);
    line-height: 1.3;
  }

  .card-meta {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
  }
</style>
