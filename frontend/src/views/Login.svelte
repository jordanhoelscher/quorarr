<script lang="ts">
  /**
   * The gate — and, since 0.2.0, the door someone knocks on.
   *
   * An account that authenticated with Plex but has no share on the server
   * comes back here with `?denied=1` and a short-lived guest cookie. Rather
   * than a dead end, the screen asks the backend where that account stands
   * and offers the one action that can change it: Request access. Everything
   * below the notice is the *same* button slot, relabelled per state — the
   * gate has one action at a time, and which one it is should never be a
   * question.
   */
  import { onMount } from 'svelte';

  import { ApiError, api } from '../lib/api';
  import { branding, unreachable } from '../lib/branding.svelte';
  import { startLogin } from '../lib/plexLogin';
  import { session } from '../lib/session.svelte';
  import type { AccessRequestState, AccessRequestStatus } from '../lib/types';

  let starting = $state(false);
  let error = $state<string | null>(null);

  /** `null` until the status call answers; only ever loaded on the denied path. */
  let access = $state<AccessRequestState | null>(null);
  let requesting = $state(false);
  /**
   * The guest cookie lapsed (15 minutes) before they pressed anything. There
   * is nothing to recover — signing in again mints a fresh one — so this
   * collapses back to the plain denial copy plus that instruction.
   */
  let expired = $state(false);

  /**
   * Prefer the error from the sign-in attempt the user just made; fall back to
   * whatever broke during the session check. Without the fallback a backend
   * outage looks identical to being signed out, which sends people hunting for
   * a Plex problem that isn't theirs.
   */
  const message = $derived(error ?? session.error);

  /**
   * The callback sends the browser back with `?denied=1` when the Plex account
   * authenticated fine but has no share on our server — a different failure
   * from "login broke", and the one most likely to actually happen.
   */
  const denied =
    typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('denied');

  /**
   * Copy for the notice, keyed off where the request stands.
   *
   * Every line except the declined one is a step in a flow that is still
   * going somewhere, so all of them say "yet" or name the next move. This is
   * the first thing a new friend ever reads from us; being turned away at a
   * door is unpleasant enough without the wording implying they did something
   * wrong.
   */
  const deniedCopy = $derived.by((): string => {
    if (expired) {
      return `That Plex account isn't on this server yet. Sign in again to ask ${branding.ownerName} to let you in.`;
    }
    switch (access) {
      case 'none':
        return `This Plex account isn't on this server yet. Ask to be let in and ${branding.ownerName} gets a nudge.`;
      case 'pending':
        return `Request sent — ${branding.ownerName} will get a nudge. Check back soon.`;
      case 'approved':
        // Since 0.5.2 an approved account is let in on the strength of the
        // approval alone, so this no longer tells anyone to go accept a Plex
        // invite before signing in -- that instruction was wrong the moment
        // the gate stopped asking plex.tv first. The invite still matters for
        // actually playing something, which is what it now says.
        return "You're in — sign in again. Plex also emailed you a library invite; accept that when you want to watch something.";
      case 'denied':
        // Pronoun-free on purpose: OWNER_NAME can be a name, a role word, or
        // a household. "have a word with them" was the alternative and reads
        // as an instruction to go and argue.
        return `${branding.ownerName} turned this one down. Nothing more to do here — get in touch if that seems wrong.`;
      default:
        return 'Checking whether that Plex account is on this server…';
    }
  });

  /**
   * The only state that has earned a warning colour, and the only one with no
   * button: the owner said no, so there is nothing left to press. Everywhere
   * else the screen is mid-flow, and dressing that as an error is what made a
   * brand-new friend think they had broken something.
   */
  const refused = $derived(denied && !expired && access === 'denied');
  const showAction = $derived(!refused);
  const showRequest = $derived(denied && !expired && access === 'none');
  const checking = $derived(denied && !expired && access === null && error === null);

  const loadAccess = async (): Promise<void> => {
    try {
      const status = await api.get<AccessRequestStatus>('/api/guest/access-requests/me');
      access = status.state;
    } catch (err) {
      // 401 means the guest cookie is gone or lapsed, which is the expected
      // shape of "they came back to this URL an hour later" — not an error
      // worth alarming anyone about.
      if (err instanceof ApiError && err.status === 401) expired = true;
      else error = err instanceof Error ? err.message : unreachable();
    }
  };

  onMount(() => {
    if (!denied) return;

    // Read once, then drop it. `?denied=1` describes a single redirect that
    // just happened -- it is not a property of this page, and leaving it in
    // the address bar makes it behave like one: it survives a reload, a
    // bookmark, a restored tab, and an iOS "Add to Home Screen" (which pins
    // whatever URL is open, not the manifest's start_url). That is how a
    // friend ends up being greeted by a refusal that stopped being true days
    // ago -- which is exactly what happened on 2026-08-14, to someone who had
    // been approved twenty minutes earlier. `replaceState` rather than
    // `pushState` so Back cannot walk into it either, and the URL is rebuilt
    // so any other param (`?tab=`) survives.
    const url = new URL(window.location.href);
    url.searchParams.delete('denied');
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);

    void loadAccess();
  });

  const requestAccess = async (): Promise<void> => {
    requesting = true;
    error = null;
    try {
      const status = await api.post<AccessRequestStatus>('/api/guest/access-requests');
      access = status.state;
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) access = 'denied';
      else if (err instanceof ApiError && err.status === 401) expired = true;
      else if (err instanceof ApiError && err.status === 400) {
        error = "That Plex account has no email address, so there's nothing to send an invite to.";
      } else {
        error = err instanceof Error ? err.message : unreachable();
      }
    } finally {
      requesting = false;
    }
  };

  const signIn = async (): Promise<void> => {
    starting = true;
    error = null;
    try {
      // Minted in this browser where possible, so the "new device" Plex
      // emails about is the friend's own — see lib/plexLogin.
      const authUrl = await startLogin();
      window.location.assign(authUrl);
      // Deliberately leave `starting` true: the page is navigating away, and
      // resetting it would flash the idle button during the redirect.
    } catch (err) {
      error = err instanceof Error ? err.message : unreachable();
      starting = false;
    }
  };
</script>

<main class="gate">
  <!-- The basin: memory-vapour turning in stone. The one moment of spectacle. -->
  <div class="basin" aria-hidden="true">
    <span class="swirl"></span>
    <span class="rim"></span>
    <span class="core"></span>
  </div>

  <h1 class="wordmark">{branding.appName}</h1>
  {#if branding.serverName}
    <p class="eyebrow origin">{branding.serverName}</p>
  {/if}

  <p class="tagline">
    What&rsquo;s on the server, what&rsquo;s coming,<br />and what&rsquo;s on its way out.
  </p>

  {#if denied}
    <!-- `doorway`, not `gate`: <main> already owns `.gate`, and reusing it
         here silently handed the notice `min-height: 100dvh`. -->
    <p class="notice doorway" class:refused role="status">
      {deniedCopy}
    </p>
  {/if}

  {#if message}
    <p class="notice error" role="alert">{message}</p>
  {/if}

  {#if showAction}
    {#if showRequest}
      <button class="signin" onclick={requestAccess} disabled={requesting}>
        <span class="signin-glow" aria-hidden="true"></span>
        <span class="signin-label">{requesting ? 'Sending…' : 'Request access'}</span>
      </button>
    {:else}
      <button class="signin" onclick={signIn} disabled={starting || checking}>
        <span class="signin-glow" aria-hidden="true"></span>
        <span class="signin-label">
          {#if checking}Checking…{:else if starting}Opening Plex…{:else if access === 'approved'}Sign in again{:else}Sign in with Plex{/if}
        </span>
      </button>
    {/if}
  {/if}

  <p class="fineprint mono">plex.tv handles the password &middot; {branding.appName} never sees it</p>
</main>

<style>
  .gate {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 100dvh;
    padding: var(--sp-7) var(--sp-5) calc(var(--sp-7) + env(safe-area-inset-bottom));
    text-align: center;
  }

  /* Everything in the gate arrives on one orchestrated cascade. */
  .basin,
  .wordmark,
  .origin,
  .tagline,
  .notice,
  .signin,
  .fineprint {
    animation: rise var(--dur-slow) var(--ease) both;
  }

  .basin {
    animation-delay: 0ms;
  }
  .wordmark {
    animation-delay: 90ms;
  }
  .origin {
    animation-delay: 150ms;
  }
  .tagline {
    animation-delay: 210ms;
  }
  .notice {
    animation-delay: 260ms;
  }
  .signin {
    animation-delay: 320ms;
  }
  .fineprint {
    animation-delay: 400ms;
  }

  /* ------------------------------------------------------------- basin */

  .basin {
    position: relative;
    width: min(15rem, 52vw);
    aspect-ratio: 1;
    margin-bottom: calc(var(--sp-5) * -1);
    display: grid;
    place-items: center;
    isolation: isolate;
  }

  .basin > span {
    position: absolute;
    inset: 0;
    border-radius: var(--r-full);
  }

  /* Conic vapour, faded to nothing at the centre so it reads as a rotating ring. */
  .swirl {
    background: conic-gradient(
      from 0deg,
      transparent 0%,
      rgb(var(--memory-rgb) / 45%) 12%,
      transparent 34%,
      rgb(90 140 200 / 35%) 55%,
      transparent 72%,
      rgb(var(--memory-rgb) / 30%) 88%,
      transparent 100%
    );
    mask: radial-gradient(closest-side, transparent 52%, #000 74%, transparent 100%);
    filter: blur(6px);
    animation: swirl 24s linear infinite;
  }

  .rim {
    border: 1px solid var(--edge-glow);
    box-shadow:
      inset 0 0 40px -14px rgb(var(--memory-rgb) / 50%),
      0 0 60px -24px rgb(var(--memory-rgb) / 70%);
  }

  .core {
    inset: 32%;
    background: radial-gradient(circle, rgb(var(--memory-rgb) / 22%), transparent 70%);
    animation: breathe 5s var(--ease) infinite;
  }

  /* -------------------------------------------------------- identity */

  .wordmark {
    margin: 0;
    font-family: var(--font-display);
    font-size: var(--fs-display);
    letter-spacing: -0.03em;
    background: linear-gradient(168deg, #ffffff 4%, var(--vapor) 34%, var(--memory-deep) 128%);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }

  .origin {
    margin-top: calc(var(--sp-2) * -1);
    color: var(--memory-deep);
    letter-spacing: 0.42em;
    text-indent: 0.42em; /* keep tracking from shifting the optical centre */
  }

  .tagline {
    max-width: 26ch;
    margin-top: var(--sp-5);
    color: var(--rune);
    font-size: var(--fs-md);
    line-height: 1.45;
    text-wrap: balance;
  }

  /* --------------------------------------------------------- notices */

  .notice {
    max-width: 34ch;
    margin-top: var(--sp-5);
    padding: var(--sp-3) var(--sp-4);
    border-radius: var(--r-md);
    font-size: var(--fs-sm);
    line-height: 1.5;
    text-align: left;
    border: 1px solid;
  }

  /* The door's own voice: cool, like everything else on this screen. Being
     told you aren't on the server yet is information, not a fault, and it is
     the first sentence most friends ever read here. */
  .doorway {
    border-color: rgb(var(--memory-rgb) / 28%);
    background: var(--memory-wash);
    color: var(--vapor);
  }

  /* Warm light means consequence, so it is spent on the single state that has
     one: the owner said no and the screen offers no button. Anything still
     mid-flow stays cool above. */
  .doorway.refused {
    border-color: rgb(var(--rose-rgb) / 30%);
    background: rgb(var(--rose-rgb) / 8%);
    color: #ffd3dc;
  }

  .error {
    border-color: rgb(242 167 95 / 32%);
    background: rgb(242 167 95 / 8%);
    color: #ffe0c2;
  }

  /* ---------------------------------------------------------- action */

  .signin {
    position: relative;
    overflow: hidden;
    margin-top: var(--sp-6);
    padding: var(--sp-4) var(--sp-7);
    border-radius: var(--r-full);
    border: 1px solid rgb(var(--memory-rgb) / 45%);
    background:
      linear-gradient(180deg, rgb(var(--memory-rgb) / 18%), rgb(var(--memory-rgb) / 7%)),
      var(--basin-hi);
    color: var(--vapor);
    font-size: var(--fs-md);
    font-weight: 500;
    letter-spacing: 0.01em;
    box-shadow:
      var(--shadow-lift),
      inset 0 1px 0 rgb(255 255 255 / 12%);
    transition:
      transform var(--dur-fast) var(--ease),
      box-shadow var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease);
  }

  .signin-label {
    position: relative;
    z-index: 1;
  }

  /* A slow sheen crossing the button — the surface of the basin catching light. */
  .signin-glow {
    position: absolute;
    inset: -60% -10%;
    background: radial-gradient(
      ellipse at 50% 50%,
      rgb(var(--memory-rgb) / 30%),
      transparent 62%
    );
    opacity: 0;
    transition: opacity var(--dur) var(--ease);
  }

  .signin:hover:not(:disabled),
  .signin:focus-visible {
    transform: translateY(-2px);
    border-color: rgb(var(--memory-rgb) / 75%);
    box-shadow:
      var(--shadow-lift),
      var(--glow-memory),
      inset 0 1px 0 rgb(255 255 255 / 18%);
  }

  .signin:hover:not(:disabled) .signin-glow {
    opacity: 1;
  }

  .signin:active:not(:disabled) {
    transform: translateY(0);
  }

  .signin:disabled {
    cursor: progress;
    color: var(--rune);
  }

  .fineprint {
    margin-top: var(--sp-4);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
  }
</style>
