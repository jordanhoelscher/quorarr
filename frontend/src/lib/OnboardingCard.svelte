<script lang="ts">
  /**
   * The one time the app asks for anything.
   *
   * Shown once per device, directly above whatever view is open, because the
   * bell in the top bar is a 16px icon that nobody has ever noticed on their
   * own. Two shapes:
   *
   * - **offer** — a browser that could subscribe right now and has never been
   *   asked. The button is the OS gesture, so `enablePush` runs straight off
   *   the click with nothing awaited in front of it.
   * - **install** — iOS in a tab, where push is impossible no matter what this
   *   card does. It shows the one instruction that unblocks it instead, saying
   *   the same thing the bell says, at length.
   *
   * Anything else — already subscribed, already refused, no push support,
   * already dismissed — renders nothing at all. A card that reappears is a
   * nag, and this one only gets a single turn: "Not now" is remembered
   * forever, and so is a refusal at the OS prompt, since the bell remains the
   * manual way in.
   */
  import { onMount } from 'svelte';
  import { branding } from './branding.svelte';

  import { enablePush, isOnboarded, markOnboarded } from './push';
  import { pushState } from './pushState.svelte';
  import { toasts } from './toast.svelte';

  /** False from the first render on a device that already had its turn. */
  let offered = $state(!isOnboarded());
  let busy = $state(false);

  onMount(() => void pushState.detect());

  /**
   * `install` speaks for itself. `off` additionally requires that the browser
   * has never been asked: a granted-but-unsubscribed browser raises no prompt,
   * so an "Enable notifications" button there would be a silent one.
   */
  const shape = $derived.by((): 'offer' | 'install' | null => {
    if (!offered) return null;
    if (pushState.mode === 'install') return 'install';
    if (pushState.mode === 'off' && pushState.permission === 'default') return 'offer';
    return null;
  });

  /** Close for good. Every exit from this card comes through here. */
  const dismiss = (): void => {
    markOnboarded();
    offered = false;
  };

  const enable = async (): Promise<void> => {
    if (busy) return;
    busy = true;
    try {
      const granted = await enablePush();
      pushState.settle(granted);
      toasts.push(
        granted
          ? "Notifications on. You'll hear about approvals and vetoes."
          : 'Left off — the bell up top turns them on any time.',
        granted ? 'info' : 'warn',
      );
      // Dismissed either way: the question has now been asked and answered,
      // and re-offering after a refusal is exactly the nagging this avoids.
      dismiss();
    } catch (err) {
      // A failure is not an answer — the offer stands, so the card stays.
      toasts.push(
        err instanceof Error ? err.message : 'Could not turn notifications on.',
        'warn',
      );
      busy = false;
    }
  };
</script>

{#if shape}
  <aside class="card panel">
    <span class="glyph" aria-hidden="true">
      {#if shape === 'offer'}
        <svg viewBox="0 0 24 24">
          <path
            d="M12 3.5a5 5 0 0 0-5 5v3l-1.5 3h13l-1.5-3v-3a5 5 0 0 0-5-5Zm-2.4 11a2.4 2.4 0 0 0 4.8 0"
          />
        </svg>
      {:else}
        <svg viewBox="0 0 24 24">
          <path d="M12 3v11m0-11-3.5 3.5M12 3l3.5 3.5M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5" />
        </svg>
      {/if}
    </span>

    <div class="body">
      {#if shape === 'offer'}
        <p class="eyebrow">Before you wander off</p>
        <h3>Let the basin reach you.</h3>
        <p class="copy">
          Turn notifications on and {branding.appName} will find you when something needs your
          veto — a film marked for deletion, a request that finally landed. Nothing else, ever.
        </p>

        <div class="acts">
          <button class="yes" disabled={busy} onclick={enable}>
            {busy ? 'Asking…' : 'Enable notifications'}
          </button>
          <button class="no" disabled={busy} onclick={dismiss}>Not now</button>
        </div>

        <p class="foot">The bell at the top does this too, whenever you change your mind.</p>
      {:else}
        <p class="eyebrow">One step first</p>
        <h3>Add {branding.appName} to your Home Screen.</h3>
        <p class="copy">
          iPhone only lets installed apps send notifications. Tap
          <span class="inline-glyph" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <path
                d="M12 3v11m0-11-3.5 3.5M12 3l3.5 3.5M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5"
              />
            </svg>
          </span>
          <strong>Share</strong> in Safari, choose <strong>Add to Home Screen</strong>, then open
          {branding.appName} from there — the bell will work from that copy.
        </p>

        <div class="acts">
          <button class="no" onclick={dismiss}>Got it</button>
        </div>
      {/if}
    </div>
  </aside>
{/if}

<style>
  .card {
    display: flex;
    gap: var(--sp-4);
    margin-bottom: var(--sp-5);
    padding: var(--sp-5);
    animation: rise var(--dur-slow) var(--ease) both;
  }

  /* The only lit thing on the page until it is answered. */
  .glyph {
    display: grid;
    place-items: center;
    flex: none;
    width: 2.5rem;
    height: 2.5rem;
    border: 1px solid rgb(var(--memory-rgb) / 30%);
    border-radius: var(--r-full);
    background: var(--memory-wash);
    box-shadow: var(--glow-memory);
    color: var(--memory);
  }

  .glyph svg,
  .inline-glyph svg {
    width: 18px;
    height: 18px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .inline-glyph {
    display: inline-grid;
    place-items: center;
    width: 1.15em;
    height: 1.15em;
    vertical-align: -0.2em;
    color: var(--memory);
  }

  .inline-glyph svg {
    width: 100%;
    height: 100%;
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    min-width: 0;
  }

  h3 {
    font-size: var(--fs-md);
    text-wrap: balance;
  }

  .copy {
    color: var(--rune);
    font-size: var(--fs-sm);
    text-wrap: pretty;
  }

  strong {
    color: var(--vapor);
    font-weight: 500;
  }

  .acts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-3);
    margin-top: var(--sp-2);
  }

  .yes {
    padding: var(--sp-2) var(--sp-5);
    border: 1px solid rgb(var(--memory-rgb) / 45%);
    border-radius: var(--r-full);
    background: var(--memory-wash);
    color: var(--memory);
    font-size: var(--fs-sm);
    transition: background-color var(--dur-fast) var(--ease);
  }

  .yes:hover:not(:disabled) {
    background: rgb(var(--memory-rgb) / 18%);
  }

  .no {
    padding: var(--sp-2) var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-sm);
    transition:
      color var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease);
  }

  .no:hover:not(:disabled) {
    color: var(--vapor);
    border-color: var(--edge-hi);
  }

  button:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .foot {
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  @media (width < 30rem) {
    .card {
      padding: var(--sp-4);
    }

    /* On a phone the icon is decoration competing with the sentence. */
    .glyph {
      display: none;
    }
  }
</style>
