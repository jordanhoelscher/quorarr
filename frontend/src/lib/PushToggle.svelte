<script lang="ts">
  /**
   * The notification bell.
   *
   * One control, five outcomes, and only one of them is a toggle:
   *
   * - **unsupported** — nothing renders. A browser that cannot do push should
   *   not be offered a switch that silently does nothing.
   * - **install** — iOS refuses `pushManager.subscribe` outside a Home Screen
   *   app, and the failure is a bare exception with no explanation. So the
   *   toggle is replaced by the one instruction that actually unblocks it.
   * - **denied** — permission was refused for this origin. The browser will
   *   not re-prompt, so tapping cannot help; the bell shows struck through and
   *   says where to undo it.
   * - **off / on** — the real toggle. The first tap is what raises the
   *   permission prompt, which is why `enablePush` runs straight off this
   *   click with nothing awaited in front of it.
   *
   * The state itself lives in `pushState`, not here: the onboarding card can
   * subscribe too, and the bell has to light up when it does.
   */
  import { onMount } from 'svelte';

  import { disablePush, enablePush } from './push';
  import { pushState } from './pushState.svelte';
  import { toasts } from './toast.svelte';

  let busy = $state(false);

  const mode = $derived(pushState.mode);

  onMount(() => void pushState.detect());

  const toggle = async (): Promise<void> => {
    if (busy) return;
    busy = true;
    try {
      if (mode === 'on') {
        await disablePush();
        pushState.settle(false);
        toasts.push('Notifications off.');
        return;
      }
      // Nothing awaited before this: iOS only honours the permission prompt
      // inside the user gesture that opened it.
      const granted = await enablePush();
      pushState.settle(granted);
      if (granted) {
        toasts.push("Notifications on. You'll hear about approvals and vetoes.");
      } else {
        toasts.push('Your browser turned notifications down.', 'warn');
      }
    } catch (err) {
      toasts.push(
        err instanceof Error ? err.message : 'Could not change notifications.',
        'warn',
      );
    } finally {
      busy = false;
    }
  };

  const BELL =
    'M12 3.5a5 5 0 0 0-5 5v3l-1.5 3h13l-1.5-3v-3a5 5 0 0 0-5-5Zm-2.4 11a2.4 2.4 0 0 0 4.8 0';

  const label = $derived(
    mode === 'on' ? 'Turn notifications off' : 'Turn notifications on',
  );
</script>

{#if mode === 'install'}
  <span class="hint">Install to Home Screen to enable notifications</span>
{:else if mode === 'denied'}
  <span class="bell denied" title="Notifications are blocked for this site — re-allow them in your browser settings.">
    <svg class="bell-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d={BELL} />
      <path d="M4 20 20 4" />
    </svg>
    <span class="sr-only">Notifications are blocked in your browser settings</span>
  </span>
{:else if mode === 'off' || mode === 'on'}
  <button
    class="bell"
    class:lit={mode === 'on'}
    aria-pressed={mode === 'on'}
    aria-label={label}
    title={label}
    disabled={busy}
    onclick={toggle}
  >
    <svg class="bell-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d={BELL} />
    </svg>
  </button>
{/if}

<style>
  .bell {
    flex: none;
    display: grid;
    place-items: center;
    width: 2rem;
    height: 2rem;
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    transition:
      color var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease),
      box-shadow var(--dur) var(--ease);
  }

  button.bell:hover:not(:disabled) {
    color: var(--vapor);
    border-color: var(--edge-hi);
  }

  button.bell:disabled {
    opacity: 0.55;
    cursor: default;
  }

  /* Lit means "this line is open" — the same cold accent the active tab uses. */
  .bell.lit {
    color: var(--memory);
    border-color: rgb(var(--memory-rgb) / 30%);
    background: var(--memory-wash);
    box-shadow: var(--glow-memory);
  }

  .bell.denied {
    color: var(--rune-dim);
    opacity: 0.7;
  }

  .bell-icon {
    width: 16px;
    height: 16px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .hint {
    max-width: 11rem;
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    line-height: 1.3;
    text-align: right;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>
