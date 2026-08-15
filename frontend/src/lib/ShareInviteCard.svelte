<script lang="ts">
  /**
   * For the friend who is approved but not yet on the server.
   *
   * Since 0.5.2 an owner's approval alone gets someone in here, which is
   * right — the Plex invite stays pending until they open the email, and
   * requiring it at the door locked out people who had already been let in.
   * The cost is a gap: they can browse everything and every request fails,
   * because Jellyseerr has no account for someone the server does not share
   * with. Previously that surfaced as "couldn't map your account — ask
   * the owner", which is jargon *and* the wrong instruction, since they had
   * already approved them.
   *
   * Shown only for `pending`. For a member who is actually on the server it
   * would be a permanent false alarm, which is worse than the silence it
   * replaces — and `unknown` (plex.tv unreadable) says nothing at all rather
   * than guessing.
   */
  import { onMount } from 'svelte';

  import { api } from './api';
  import type { ShareState, ShareStatus } from './types';

  /**
   * `sessionStorage`, not component state: `App.svelte` wraps views in
   * `{#key activeTab.id}` and destroys them on every tab switch, so a
   * dismissal held in a rune would evaporate the moment they looked at
   * Library and came back — turning one notice into a nag on every
   * navigation. sessionStorage lasts exactly as long as the sitting.
   */
  const DISMISSED_KEY = 'share-invite-dismissed';

  const wasDismissed = (): boolean => {
    try {
      return sessionStorage.getItem(DISMISSED_KEY) === '1';
    } catch {
      return false;
    }
  };

  // Not named `state`: that shadows the `$state` rune and TypeScript then
  // reads the declaration as its own initializer.
  let shareState = $state<ShareState | null>(null);
  let dismissed = $state(wasDismissed());

  const show = $derived(shareState === 'pending' && !dismissed);

  const dismiss = (): void => {
    dismissed = true;
    try {
      sessionStorage.setItem(DISMISSED_KEY, '1');
    } catch {
      // Private mode can refuse storage. The in-memory flag still hides it
      // for this view, which is the whole of what dismissal promises.
    }
  };

  onMount(() => {
    void (async () => {
      try {
        shareState = (await api.get<ShareStatus>('/api/me/share')).state;
      } catch {
        // A card that exists to explain a problem must never become one.
        shareState = null;
      }
    })();
  });
</script>

{#if show}
  <aside class="card panel">
    <span class="glyph" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <path d="M3 7.5h18v11a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-11Zm0 0L12 14l9-6.5" />
      </svg>
    </span>

    <div class="body">
      <p class="eyebrow mono">One more step</p>
      <h3>Accept your Plex invite</h3>
      <p class="says">
        You&rsquo;re in — but the server hasn&rsquo;t been shared with your Plex account yet. Plex
        emailed you an invite; accepting it is what lets you play things and ask for new ones.
      </p>
      <button class="dismiss" onclick={dismiss}>Got it</button>
    </div>
  </aside>
{/if}

<style>
  .card {
    display: flex;
    gap: var(--sp-4);
    margin-bottom: var(--sp-5);
    animation: rise var(--dur-slow) var(--ease) both;
  }

  .glyph {
    flex: none;
    display: grid;
    place-items: center;
    width: 2.5rem;
    height: 2.5rem;
    border-radius: var(--r-full);
    background: var(--memory-wash);
  }

  .glyph svg {
    width: 20px;
    height: 20px;
    fill: none;
    stroke: var(--memory);
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .body {
    min-width: 0;
  }

  .eyebrow {
    margin: 0;
    color: var(--memory);
    font-size: var(--fs-micro);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  h3 {
    margin: var(--sp-1) 0 var(--sp-2);
    font-family: var(--font-display);
    font-size: var(--fs-lg);
    letter-spacing: -0.01em;
    color: var(--vapor);
  }

  .says {
    margin: 0 0 var(--sp-4);
    color: var(--rune);
    font-size: var(--fs-sm);
    line-height: 1.5;
  }

  .dismiss {
    padding: var(--sp-2) var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-sm);
    transition:
      color var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease);
  }

  .dismiss:hover {
    color: var(--vapor);
    border-color: var(--edge-hi);
  }
</style>
