<script lang="ts">
  /**
   * Renders the toast stack. Mounted once, in the app shell.
   *
   * Anchored to the top rather than the bottom: the action sheet and the
   * phone tab bar both own the bottom of the screen, and a toast that appears
   * behind a sheet is a toast nobody reads.
   */
  import { toasts } from './toast.svelte';
</script>

<div class="toaster" role="status" aria-live="polite">
  {#each toasts.items as toast (toast.id)}
    <button class="toast" class:warn={toast.tone === 'warn'} onclick={() => toasts.dismiss(toast.id)}>
      <span class="dot" aria-hidden="true"></span>
      <span class="message">{toast.message}</span>
    </button>
  {/each}
</div>

<style>
  .toaster {
    position: fixed;
    inset: 0 0 auto;
    z-index: 60;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--sp-2);
    padding: calc(var(--sp-3) + env(safe-area-inset-top)) var(--sp-4) 0;
    pointer-events: none;
  }

  .toast {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    max-width: min(30rem, 100%);
    padding: var(--sp-3) var(--sp-4);
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-full);
    background: rgb(14 20 29 / 92%);
    backdrop-filter: blur(16px) saturate(140%);
    -webkit-backdrop-filter: blur(16px) saturate(140%);
    box-shadow: var(--shadow-lift);
    color: var(--vapor);
    font-size: var(--fs-sm);
    text-align: left;
    pointer-events: auto;
    animation: rise var(--dur) var(--ease) both;
  }

  .dot {
    flex: none;
    width: 6px;
    height: 6px;
    border-radius: var(--r-full);
    background: var(--memory);
    box-shadow: var(--glow-memory);
  }

  /* Warm light means something did not happen. */
  .toast.warn {
    border-color: rgb(var(--rose-rgb) / 34%);
  }

  .toast.warn .dot {
    background: var(--ember);
    box-shadow: 0 0 18px -2px rgb(242 167 95 / 60%);
  }

  .message {
    min-width: 0;
  }
</style>
