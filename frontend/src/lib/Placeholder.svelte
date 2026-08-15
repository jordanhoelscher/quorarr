<script lang="ts">
  /**
   * The three non-content states a view can be in.
   *
   * Every view uses this rather than rendering nothing: a blank panel is
   * indistinguishable from a broken one, and "the server is down" is a
   * different message from "there is nothing here yet".
   */
  interface Props {
    tone: 'loading' | 'error' | 'empty';
    message: string;
    /** Shown as a "Try again" button on the error state. */
    onretry?: () => void;
  }

  const { tone, message, onretry }: Props = $props();
</script>

<div class="placeholder panel" class:error={tone === 'error'}>
  {#if tone === 'loading'}
    <span class="pulse" aria-hidden="true"></span>
  {:else if tone === 'error'}
    <span class="mark" aria-hidden="true">!</span>
  {/if}

  <p class="message">{message}</p>

  {#if onretry}
    <button class="retry" onclick={onretry}>Try again</button>
  {/if}
</div>

<style>
  .placeholder {
    display: flex;
    align-items: center;
    gap: var(--sp-4);
    padding: var(--sp-6) var(--sp-5);
  }

  .pulse {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: var(--r-full);
    background: var(--memory);
    box-shadow: var(--glow-memory);
    animation: breathe 2.8s var(--ease) infinite;
  }

  /* Warm light: something failed. */
  .mark {
    display: grid;
    place-items: center;
    flex: none;
    width: 22px;
    height: 22px;
    border: 1px solid rgb(var(--rose-rgb) / 40%);
    border-radius: var(--r-full);
    color: var(--ember);
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
  }

  .placeholder.error {
    border-color: rgb(var(--rose-rgb) / 22%);
  }

  .message {
    flex: 1;
    min-width: 0;
    color: var(--rune);
    font-size: var(--fs-sm);
    text-wrap: balance;
  }

  .retry {
    flex: none;
    padding: var(--sp-2) var(--sp-4);
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-full);
    color: var(--vapor);
    font-size: var(--fs-xs);
    transition:
      border-color var(--dur-fast) var(--ease),
      background-color var(--dur-fast) var(--ease);
  }

  .retry:hover {
    border-color: var(--edge-glow);
    background: var(--memory-wash);
  }
</style>
