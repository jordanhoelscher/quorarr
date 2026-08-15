<script lang="ts">
  import { onMount, type Component } from 'svelte';

  import OnboardingCard from './lib/OnboardingCard.svelte';
  import ShareInviteCard from './lib/ShareInviteCard.svelte';
  import PushToggle from './lib/PushToggle.svelte';
  import Toaster from './lib/Toaster.svelte';
  import { branding, loadBranding } from './lib/branding.svelte';
  import { session } from './lib/session.svelte';
  import Discover from './views/Discover.svelte';
  import Login from './views/Login.svelte';
  import Storage from './views/Storage.svelte';
  import Pipeline from './views/Pipeline.svelte';
  import Library from './views/Library.svelte';
  import Flagged from './views/Flagged.svelte';
  import Approvals from './views/Approvals.svelte';

  type TabId = 'discover' | 'storage' | 'pipeline' | 'library' | 'flagged' | 'approvals';

  interface Tab {
    id: TabId;
    label: string;
    /** SVG path data, drawn at 24x24 with a 1.6 stroke. */
    icon: string;
    component: Component;
    /** Owner-only sections are absent for members, not disabled. */
    ownerOnly?: boolean;
  }

  const TABS: readonly Tab[] = [
    {
      id: 'discover',
      label: 'Discover',
      icon: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14ZM16 16l4.5 4.5',
      component: Discover,
    },
    {
      id: 'pipeline',
      label: 'Pipeline',
      icon: 'M12 3v10m0 0 4-4m-4 4-4-4M4 16v2a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3v-2',
      component: Pipeline,
    },
    {
      id: 'library',
      label: 'Library',
      icon: 'M3 6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6ZM8 4v16M16 4v16M3 9h5M3 15h5M16 9h5M16 15h5',
      component: Library,
    },
    {
      id: 'flagged',
      label: 'Flagged',
      icon: 'M5 21V4m0 0h11l-2.2 4L16 12H5',
      component: Flagged,
    },
    {
      id: 'storage',
      label: 'Storage',
      icon: 'M4 6c0 1.66 3.58 3 8 3s8-1.34 8-3-3.58-3-8-3-8 1.34-8 3ZM4 6v12c0 1.66 3.58 3 8 3s8-1.34 8-3V6M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3',
      component: Storage,
    },
    {
      id: 'approvals',
      label: 'Approvals',
      icon: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-3.5 9.4 2.6 2.6 4.4-5',
      component: Approvals,
      ownerOnly: true,
    },
  ];

  /**
   * A notification tap lands on `/?tab=<id>` (see `sw.js`), so the deep link
   * is read once at start-up. An unknown or absent value falls back to the
   * first tab rather than erroring — the query string is user-editable, and a
   * stale link from an older release must not strand anyone on a blank view.
   * The fallback reads `TABS[0]` rather than naming a tab, so reordering the
   * bar (as v0.4.0 did, putting Discover in front) moves the landing tab with
   * it instead of quietly stranding a constant.
   */
  const initialTab = (): TabId => {
    if (typeof window === 'undefined') return TABS[0].id;
    const wanted = new URLSearchParams(window.location.search).get('tab');
    return TABS.some((tab) => tab.id === wanted) ? (wanted as TabId) : TABS[0].id;
  };

  let activeId = $state<TabId>(initialTab());

  const isOwner = $derived(session.user?.role === 'owner');
  const tabs = $derived(TABS.filter((tab) => !tab.ownerOnly || isOwner));
  /**
   * Fall back to the first tab rather than trusting `activeId`: a member
   * signing in after an owner would otherwise sit on a tab that no longer
   * exists for them.
   */
  const activeTab = $derived(tabs.find((tab) => tab.id === activeId) ?? tabs[0]);
  const ActiveView = $derived(activeTab.component);

  onMount(() => {
    // Concurrent, not sequential: both are answered while the splash is up,
    // and neither needs the other's result.
    void loadBranding();
    void session.load();
  });
</script>

{#if session.loading}
  <!-- Splash, not a spinner: the cookie check is usually one fast round trip. -->
  <div class="splash">
    <!-- Held invisible (not absent, so nothing jumps) until /api/config says
         what this instance is called. -->
    <span class="splash-mark" class:pending={!branding.resolved}>{branding.appName}</span>
    <span class="splash-pulse" aria-hidden="true"></span>
  </div>
{:else if !session.user}
  <Login />
{:else}
  <div class="shell">
    <header class="topbar">
      <span class="brand">{branding.appName}</span>

      <div class="who">
        <span class="who-name">{session.user.name}</span>
        {#if isOwner}<span class="badge mono">Owner</span>{/if}
        <PushToggle />
        <button class="signout" onclick={() => session.logout()}>Sign out</button>
      </div>
    </header>

    <nav class="tabs" aria-label="Sections">
      {#each tabs as tab (tab.id)}
        <button
          class="tab"
          class:active={tab.id === activeTab.id}
          aria-current={tab.id === activeTab.id ? 'page' : undefined}
          onclick={() => (activeId = tab.id)}
        >
          <svg class="tab-icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d={tab.icon} />
          </svg>
          <span class="tab-label">{tab.label}</span>
        </button>
      {/each}
    </nav>

    <!-- Blocking first: an approved friend whose Plex invite is unaccepted can
         browse everything and request nothing, and nothing else says why. -->
    <ShareInviteCard />

    <!-- Once per device, above whatever view is open: nobody finds the bell. -->
    <OnboardingCard />

    <main class="stage">
      {#key activeTab.id}
        <ActiveView />
      {/key}
    </main>

    <footer class="footer">
      <span class="mono">{branding.appName} v{__APP_VERSION__}</span>
    </footer>
  </div>

  <!-- One stack for the whole app: every write here closes its own surface. -->
  <Toaster />
{/if}

<style>
  /* ---------------------------------------------------------- splash */

  .splash {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--sp-4);
    min-height: 100dvh;
  }

  .splash-mark.pending {
    opacity: 0;
  }

  .splash-mark {
    transition: opacity var(--dur-slow) var(--ease);
    font-family: var(--font-display);
    font-size: var(--fs-xl);
    letter-spacing: -0.02em;
    color: var(--rune);
    animation: rise var(--dur) var(--ease) both;
  }

  .splash-pulse {
    width: 6px;
    height: 6px;
    border-radius: var(--r-full);
    background: var(--memory);
    box-shadow: var(--glow-memory);
    animation: breathe 1.6s var(--ease) infinite;
  }

  /* ----------------------------------------------------------- shell */

  .shell {
    display: flex;
    flex-direction: column;
    min-height: 100dvh;
    max-width: var(--shell-max);
    margin-inline: auto;
    padding-inline: var(--sp-4);
  }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-4);
    /* Clear the iPhone Dynamic Island / status bar in standalone PWA mode
       (viewport-fit=cover pulls content under it otherwise). */
    padding-block: calc(var(--sp-5) + env(safe-area-inset-top, 0px)) var(--sp-4);
  }

  .brand {
    font-family: var(--font-display);
    font-size: var(--fs-lg);
    letter-spacing: -0.02em;
    color: var(--vapor);
  }

  .who {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    min-width: 0;
  }

  .who-name {
    font-size: var(--fs-sm);
    color: var(--rune);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .badge {
    flex: none;
    padding: 2px var(--sp-2);
    border: 1px solid rgb(var(--memory-rgb) / 30%);
    border-radius: var(--r-full);
    background: var(--memory-wash);
    color: var(--memory);
    font-size: var(--fs-micro);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .signout {
    flex: none;
    padding: var(--sp-1) var(--sp-3);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
    transition:
      color var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease);
  }

  .signout:hover {
    color: var(--vapor);
    border-color: var(--edge-hi);
  }

  /* ------------------------------------------------------------ tabs */

  .tab {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    padding: var(--sp-3) var(--sp-3);
    color: var(--rune-dim);
    font-size: var(--fs-sm);
    transition: color var(--dur-fast) var(--ease);
  }

  .tab:hover {
    color: var(--rune);
  }

  .tab.active {
    color: var(--vapor);
  }

  .tab-icon {
    width: 20px;
    height: 20px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: filter var(--dur) var(--ease);
  }

  .tab.active .tab-icon {
    color: var(--memory);
    filter: drop-shadow(0 0 6px rgb(var(--memory-rgb) / 55%));
  }

  /* The memory strand: a lit filament on the outer edge of the active tab. */
  .tab::after {
    content: '';
    position: absolute;
    inset-inline: var(--sp-3);
    height: 2px;
    border-radius: var(--r-full);
    background: linear-gradient(90deg, transparent, var(--memory), transparent);
    opacity: 0;
    transform: scaleX(0.3);
    transition:
      opacity var(--dur) var(--ease),
      transform var(--dur) var(--ease);
  }

  .tab.active::after {
    opacity: 1;
    transform: none;
    box-shadow: 0 0 10px rgb(var(--memory-rgb) / 70%);
  }

  .stage {
    flex: 1;
    padding-block: var(--sp-5);
  }

  .footer {
    padding-block: var(--sp-5);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.08em;
    text-align: center;
  }

  /* -------------------------------------------------- narrow: app-like */

  @media (width < 44rem) {
    .shell {
      /* Leave room for the fixed bar so the footer is never trapped under it. */
      padding-bottom: calc(var(--tabbar-h) + env(safe-area-inset-bottom));
    }

    .tabs {
      position: fixed;
      inset: auto 0 0;
      z-index: 20;
      display: flex;
      justify-content: space-around;
      padding-bottom: env(safe-area-inset-bottom);
      border-top: 1px solid var(--edge);
      background: rgb(9 14 21 / 82%);
      backdrop-filter: blur(18px) saturate(140%);
      -webkit-backdrop-filter: blur(18px) saturate(140%);
    }

    .tab {
      flex: 1;
      flex-direction: column;
      gap: var(--sp-1);
      height: var(--tabbar-h);
      justify-content: center;
      padding-inline: 0;
      font-size: var(--fs-micro);
    }

    /* Strand sits above the icon on the bar's outer (upper) edge. */
    .tab::after {
      inset-inline: 28%;
      top: 0;
    }

    .tab-label {
      letter-spacing: 0.02em;
    }
  }

  /* ----------------------------------------------------- wide: segmented */

  @media (width >= 44rem) {
    .tabs {
      display: flex;
      gap: var(--sp-2);
      border-bottom: 1px solid var(--edge);
    }

    .tab {
      padding-inline: var(--sp-4);
    }

    /* Strand sits on the rule below the row. */
    .tab::after {
      bottom: -1px;
    }

    .footer {
      text-align: right;
    }
  }
</style>
