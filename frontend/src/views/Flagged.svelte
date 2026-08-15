<script lang="ts">
  /**
   * Titles someone has proposed deleting, and how everyone's quality
   * requests turned out.
   *
   * This is the only view graded warm, because it is the only view where
   * something is about to be lost. **Keep** is the cold, lit action against
   * that: one tap, no confirmation, and it takes effect on screen before the
   * server answers — vetoing is meant to feel like catching something, and a
   * spinner between the tap and the save undercuts that. If the server refuses
   * (someone else already resolved it) the row comes back with the reason.
   */
  import { onMount } from 'svelte';
  import { branding, unreachable } from '../lib/branding.svelte';

  import Placeholder from '../lib/Placeholder.svelte';
  import ViewHead from '../lib/ViewHead.svelte';
  import { ApiError, api } from '../lib/api';
  import { daysLeft, formatBytes, timeAgo } from '../lib/format';
  import { toasts } from '../lib/toast.svelte';
  import type { Flag, FlagBoard, QualityRequest, QualityRequestList } from '../lib/types';

  /** Mirrors `deletion.VETO_WINDOW_DAYS` on the backend. */
  const VETO_WINDOW_DAYS = 14;
  /** Days remaining at which the countdown turns warm. */
  const URGENT_DAYS = 3;

  let board = $state<FlagBoard | null>(null);
  let error = $state<string | null>(null);
  let loading = $state(true);
  /** Flag ids with a veto in flight, so a double-tap can't send twice. */
  let vetoing = $state<number[]>([]);
  let requests = $state<QualityRequest[]>([]);
  let requestsError = $state<string | null>(null);

  const load = async (): Promise<void> => {
    loading = true;
    error = null;
    try {
      board = await api.get<FlagBoard>('/api/flags');
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        error = err instanceof ApiError ? err.message : unreachable();
      }
    } finally {
      loading = false;
    }
  };

  /**
   * Quality requests load on their own, and fail on their own.
   *
   * A request outcome is the only way a member ever learns what came of
   * asking — the deny note is written for them and had nowhere to appear
   * before this. But it is secondary to the veto window, so a failure here
   * degrades to one muted line rather than replacing the whole view with an
   * error panel and hiding flags someone may only have hours left to keep.
   */
  const loadRequests = async (): Promise<void> => {
    requestsError = null;
    try {
      const body = await api.get<QualityRequestList>('/api/quality-requests');
      requests = body.items;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return;
      requestsError = 'Could not load quality requests.';
    }
  };

  onMount(() => {
    void load();
    void loadRequests();
  });

  const keep = async (flag: Flag): Promise<void> => {
    if (!board || vetoing.includes(flag.id)) return;

    const restore = board.active;
    const index = restore.findIndex((f) => f.id === flag.id);

    vetoing = [...vetoing, flag.id];
    board = { ...board, active: restore.filter((f) => f.id !== flag.id) };

    try {
      const saved = await api.post<Flag>(`/api/flags/${flag.id}/veto`);
      // Fold the resolved row into "recent" so the decision is visible
      // immediately rather than only after the next load.
      if (board) board = { ...board, recent: [saved, ...board.recent].slice(0, 20) };
      toasts.push(`Keeping ${flag.title}`);
    } catch (err) {
      // Put it back exactly where it was — the list is ordered by flagged_at,
      // and appending would quietly reorder the board.
      if (board) {
        const next = [...board.active];
        next.splice(Math.max(0, index), 0, flag);
        board = { ...board, active: next };
      }
      toasts.push(
        err instanceof ApiError ? err.message : 'Could not save that. Try again in a moment.',
        'warn',
      );
    } finally {
      vetoing = vetoing.filter((id) => id !== flag.id);
    }
  };

  // `$derived`, not a plain const: `branding.ownerName` arrives from
  // /api/config after mount, and a notification tap can land straight on this
  // view (`?tab=flagged`) while that fetch is still in flight. A bare object
  // literal would capture "the owner" at init and never update — this view
  // only remounts on a tab change, so the stale label would survive.
  const OUTCOMES: Record<string, string> = $derived({
    vetoed: 'Kept',
    pending_approval: `Awaiting ${branding.ownerName}`,
    approved: 'Approved',
    denied: 'Denied',
    executed: 'Deleted',
  });

  /** Who closed a flag out, when the record names anyone. */
  const resolver = (flag: Flag): string | null => {
    if (flag.state === 'vetoed' && flag.vetoed_by_name) return `by ${flag.vetoed_by_name}`;
    return null;
  };

  const scopeLabel = (flag: Flag): string =>
    flag.season_number === null ? '' : `Season ${flag.season_number}`;

  /**
   * Plain-language outcome per request state — the member's words, not the
   * DB's. `$derived` for the same reason as `OUTCOMES` above.
   */
  const REQUEST_STATES: Record<string, string> = $derived({
    auto_triggered: 'Searching',
    pending_approval: `Awaiting ${branding.ownerName}`,
    approved: 'Approved',
    denied: 'Declined',
    error: 'Failed',
  });

  const requestWhen = (request: QualityRequest): string =>
    timeAgo(request.resolved_at ?? request.created_at);
</script>

<section class="view">
  <ViewHead eyebrow="Deletions and requests" title="Flagged" />

  {#if loading}
    <Placeholder tone="loading" message="Checking what is up for deletion…" />
  {:else if error}
    <Placeholder tone="error" message={error} onretry={load} />
  {:else if board}
    {#if board.active.length === 0}
      <Placeholder
        tone="empty"
        message="Nothing is up for deletion. When someone marks a title, it lands here and everyone gets 14 days to say no."
      />
    {:else}
      <ul class="flags">
        {#each board.active as flag (flag.id)}
          {@const left = daysLeft(flag.flagged_at, VETO_WINDOW_DAYS)}
          <li class="flag panel" class:urgent={left <= URGENT_DAYS}>
            <div class="flag-main">
              <span class="clock mono" class:urgent={left <= URGENT_DAYS}>
                <span class="clock-num">{left}</span>
                <span class="clock-unit">{left === 1 ? 'day' : 'days'}</span>
              </span>

              <div class="detail">
                <p class="title">{flag.title}</p>

                <p class="who">
                  {flag.flagged_by_name} · {timeAgo(flag.flagged_at)}
                  {#if scopeLabel(flag)}<span class="scope mono">{scopeLabel(flag)}</span>{/if}
                </p>

                {#if flag.reason}
                  <p class="reason">“{flag.reason}”</p>
                {/if}

                <p class="size mono">{formatBytes(flag.size_bytes)} would come back</p>
              </div>
            </div>

            <button class="keep" disabled={vetoing.includes(flag.id)} onclick={() => keep(flag)}>
              Keep
            </button>
          </li>
        {/each}
      </ul>
    {/if}

    {#if board.recent.length > 0}
      <section class="recent">
        <p class="eyebrow">Recent decisions</p>

        <ul class="decisions">
          {#each board.recent as flag (flag.id)}
            <li class="decision">
              <span
                class="outcome mono"
                class:kept={flag.state === 'vetoed'}
                class:gone={flag.state === 'executed' || flag.state === 'approved'}
              >
                {OUTCOMES[flag.state] ?? flag.state}
              </span>

              <span class="decision-title">{flag.title}</span>

              <span class="decision-when mono">
                {#if resolver(flag)}{resolver(flag)} · {/if}
                {timeAgo(flag.resolved_at ?? flag.flagged_at)}
              </span>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}

  {#if requestsError}
    <section class="recent">
      <p class="eyebrow">Quality requests</p>
      <p class="requests-error">{requestsError}</p>
    </section>
  {:else if requests.length > 0}
    <section class="recent">
      <p class="eyebrow">Quality requests</p>

      <ul class="requests">
        {#each requests as request (request.id)}
          <li class="request">
            <div class="request-head">
              <span
                class="outcome mono"
                class:kept={request.state === 'approved' || request.state === 'auto_triggered'}
                class:gone={request.state === 'denied' || request.state === 'error'}
              >
                {REQUEST_STATES[request.state] ?? request.state}
              </span>

              <span class="request-title">{request.title}</span>

              <span class="tier mono">{request.requested_quality}</span>
            </div>

            <p class="who">
              {request.requested_by_name} · {requestWhen(request)}
              {#if request.season_number !== null}
                <span class="scope mono">Season {request.season_number}</span>
              {/if}
            </p>

            {#if request.note}
              <p class="requests-note">“{request.note}”</p>
            {/if}
          </li>
        {/each}
      </ul>
    </section>
  {/if}
</section>

<style>
  .view {
    animation: rise var(--dur-slow) var(--ease) both;
  }

  .flags {
    display: flex;
    flex-direction: column;
    gap: var(--sp-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .flag {
    display: flex;
    align-items: center;
    gap: var(--sp-4);
    padding: var(--sp-4) var(--sp-5);
    /* Warm light on the edge: this row is the one thing at risk. */
    border-color: rgb(var(--rose-rgb) / 16%);
  }

  .flag.urgent {
    border-color: rgb(var(--rose-rgb) / 34%);
    background:
      linear-gradient(180deg, rgb(242 112 139 / 5%), transparent 40%),
      var(--basin);
  }

  .flag-main {
    display: flex;
    align-items: flex-start;
    gap: var(--sp-4);
    flex: 1;
    min-width: 0;
  }

  /* ------------------------------------------------------------- clock */

  .clock {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    flex: none;
    width: 3.25rem;
    height: 3.25rem;
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-full);
    color: var(--rune);
    line-height: 1;
  }

  .clock-num {
    font-size: var(--fs-md);
  }

  .clock-unit {
    font-size: 0.5625rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    opacity: 0.65;
  }

  .clock.urgent {
    border-color: rgb(var(--rose-rgb) / 50%);
    color: var(--ember);
    box-shadow: 0 0 20px -6px rgb(242 167 95 / 55%);
  }

  /* ------------------------------------------------------------ detail */

  .detail {
    min-width: 0;
  }

  .title {
    font-size: var(--fs-md);
    text-wrap: balance;
  }

  .who {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    margin-top: 2px;
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .scope {
    padding: 1px var(--sp-2);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    font-size: var(--fs-micro);
  }

  .reason {
    margin-top: var(--sp-2);
    color: var(--rune);
    font-size: var(--fs-sm);
    font-style: italic;
    text-wrap: pretty;
  }

  .size {
    margin-top: var(--sp-2);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
  }

  /* --------------------------------------------------------------- keep */

  .keep {
    flex: none;
    padding: var(--sp-3) var(--sp-5);
    border-radius: var(--r-full);
    background: var(--memory);
    color: #04222a;
    font-size: var(--fs-sm);
    font-weight: 600;
    box-shadow: var(--glow-memory);
    transition:
      transform var(--dur-fast) var(--ease),
      filter var(--dur-fast) var(--ease);
  }

  .keep:hover:not(:disabled) {
    transform: translateY(-1px);
    filter: brightness(1.08);
  }

  .keep:disabled {
    opacity: 0.55;
    cursor: default;
  }

  /* ------------------------------------------------------------ recent */

  .recent {
    margin-top: var(--sp-7);
    padding-top: var(--sp-4);
    border-top: 1px solid var(--edge);
  }

  .decisions {
    margin: var(--sp-3) 0 0;
    padding: 0;
    list-style: none;
  }

  .decision {
    display: flex;
    align-items: baseline;
    gap: var(--sp-3);
    padding-block: var(--sp-2);
    font-size: var(--fs-xs);
  }

  .outcome {
    flex: none;
    width: 7.5rem;
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .outcome.kept {
    color: var(--moss);
  }

  .outcome.gone {
    color: var(--ember);
  }

  .decision-title {
    flex: 1;
    min-width: 0;
    color: var(--rune);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .decision-when {
    flex: none;
    color: var(--rune-dim);
    font-size: var(--fs-micro);
  }

  /* -------------------------------------------------- quality requests */

  .requests {
    display: flex;
    flex-direction: column;
    gap: var(--sp-3);
    margin: var(--sp-3) 0 0;
    padding: 0;
    list-style: none;
  }

  .request-head {
    display: flex;
    align-items: baseline;
    gap: var(--sp-3);
    font-size: var(--fs-xs);
  }

  .request-title {
    flex: 1;
    min-width: 0;
    color: var(--rune);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tier {
    flex: none;
    padding: 1px var(--sp-2);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
  }

  .request .who {
    margin-top: 2px;
    padding-left: 8.5rem;
  }

  .requests-error {
    margin-top: var(--sp-3);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .requests-note {
    margin-top: var(--sp-1);
    padding-left: 8.5rem;
    color: var(--rune-dim);
    font-size: var(--fs-xs);
    font-style: italic;
    text-wrap: pretty;
  }

  @media (width < 34rem) {
    .flag {
      flex-direction: column;
      align-items: stretch;
    }

    .keep {
      width: 100%;
    }

    .outcome {
      width: 5.5rem;
    }

    .request .who,
    .requests-note {
      padding-left: 0;
    }

    .decision-when {
      display: none;
    }
  }
</style>
