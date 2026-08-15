<script lang="ts">
  /**
   * The heading every section shares: a tracked mono eyebrow over a serif
   * title, with room on the right for that section's own controls.
   */
  import type { Snippet } from 'svelte';

  interface Props {
    eyebrow: string;
    title: string;
    /** Section controls — refresh buttons, stale badges. */
    aside?: Snippet;
  }

  const { eyebrow, title, aside }: Props = $props();
</script>

<header class="view-head">
  <div class="titles">
    <p class="eyebrow">{eyebrow}</p>
    <h2>{title}</h2>
  </div>

  {#if aside}
    <div class="aside">{@render aside()}</div>
  {/if}
</header>

<style>
  .view-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--sp-4);
    margin-bottom: var(--sp-5);
  }

  .titles {
    display: flex;
    flex-direction: column;
    gap: var(--sp-1);
    min-width: 0;
  }

  h2 {
    font-size: var(--fs-xl);
  }

  .aside {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    flex: none;
    padding-bottom: var(--sp-1);
  }
</style>
