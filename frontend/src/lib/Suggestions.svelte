<script lang="ts">
  /**
   * The type-ahead list under the Discover search field.
   *
   * Two kinds of row, which is the whole reason this exists: a title, which
   * opens the detail sheet it always has, and a person, which the results
   * grid can never show because a person is not something to request.
   *
   * It owns no state and fetches nothing — `Discover` holds the query, the
   * debounce and the highlight, because those are the things the input's own
   * key handler has to move. This is the rendering and the ARIA wiring.
   *
   * `onmousedown` is prevented on the whole panel so a tap here never blurs
   * the input: blur is what closes the list, and closing it before the click
   * lands is the classic autocomplete bug where nothing is ever selectable.
   */
  import Poster from './Poster.svelte';
  import { posterUrl } from './format';
  import { isPerson, suggestKey } from './suggest';
  import type { Suggestion } from './types';

  interface Props {
    rows: Suggestion[];
    /** Index of the keyboard-highlighted row, or -1 for none. */
    active: number;
    /** Shared with the input's `aria-activedescendant`. */
    optionId: (index: number) => string;
    onpick: (row: Suggestion) => void;
    onhover: (index: number) => void;
  }

  const { rows, active, optionId, onpick, onhover }: Props = $props();
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<ul
  class="panel"
  id="discover-suggestions"
  role="listbox"
  aria-label="Search suggestions"
  onmousedown={(event) => event.preventDefault()}
>
  {#each rows as row, index (suggestKey(row))}
    <!--
      svelte-ignore a11y_click_events_have_key_events

      Correct for a combobox, and the reason the rule cannot see it: the
      keyboard never reaches these rows. Focus stays in the input, which owns
      arrows/Enter/Escape and points `aria-activedescendant` here — making
      each row a `<button>` would put them in the tab order and break exactly
      the pattern a screen reader is expecting.
    -->
    <li
      class="row"
      class:active={index === active}
      id={optionId(index)}
      role="option"
      aria-selected={index === active}
      onclick={() => onpick(row)}
      onmouseenter={() => onhover(index)}
    >
      {#if isPerson(row)}
        <Poster src={posterUrl(row.profile_path, 'w185')} size="face" />
        <span class="text">
          <span class="name">{row.name}</span>
          <span class="kind mono">Actor</span>
        </span>
      {:else}
        <Poster src={posterUrl(row.poster_path, 'w185')} size="row" />
        <span class="text">
          <span class="name">{row.title}</span>
          <span class="kind mono">
            {row.media_type === 'tv' ? 'TV' : 'Film'}{row.year ? ` · ${row.year}` : ''}
          </span>
        </span>
      {/if}
    </li>
  {/each}
</ul>

<style>
  /*
   * Hangs off the finder rather than pushing the shelves down: the list is a
   * transient reading of what was typed, and reflowing the page under it on
   * every keystroke is what makes an autocomplete feel unstable.
   */
  .panel {
    position: absolute;
    z-index: 20;
    inset: calc(100% + var(--sp-2)) 0 auto 0;
    max-height: 22rem;
    margin: 0;
    padding: var(--sp-2);
    overflow-y: auto;
    overscroll-behavior: contain;
    list-style: none;
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-md);
    background: var(--basin);
    box-shadow: var(--shadow-lift);
    animation: rise var(--dur-fast) var(--ease) both;
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    padding: var(--sp-2);
    border-radius: var(--r-sm);
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease);
  }

  /*
   * One highlight for both input methods. Hover writes the same `active`
   * index the arrow keys do, so the mouse and the keyboard can never disagree
   * about which row Enter would take.
   */
  .row.active {
    background: var(--basin-hi);
  }

  .text {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 2px;
  }

  .name {
    overflow: hidden;
    color: var(--vapor);
    font-size: var(--fs-sm);
    line-height: 1.3;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .kind {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
  }
</style>
