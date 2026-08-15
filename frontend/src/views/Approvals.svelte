<script lang="ts">
  /**
   * The owner's desk — the only screen in the app that destroys anything.
   *
   * Three queues sit here, and they are deliberately graded against each
   * other: **deletions are warm** (rose edge, ember figures) because
   * approving one removes files from disk with nothing to put them back,
   * while **quality and access requests are cold** (the house cyan) because
   * the worst case is a wasted download or a share that can be taken back in
   * Plex. Reading the colour should tell the owner what kind of decision
   * they face before reading a single word — which is the whole point
   * of having reserved warm light for consequence everywhere else in the app.
   *
   * Deleting asks twice: once on the row, once in a dialog that names the
   * title and the size. A retry after a failed execution does *not* ask again
   * — that decision was already made and recorded as `approved`; the retry is
   * resuming a committed action, not starting a new one.
   *
   * This is also the only surface allowed to render the raw `error` column.
   * It is upstream exception text, useful precisely because it is unedited,
   * and it never leaves the owner gate.
   */
  import { onMount } from 'svelte';
  import { branding, unreachable } from '../lib/branding.svelte';

  import Placeholder from '../lib/Placeholder.svelte';
  import ViewHead from '../lib/ViewHead.svelte';
  import { ApiError, api } from '../lib/api';
  import { formatBytes, timeAgo } from '../lib/format';
  import { toasts } from '../lib/toast.svelte';
  import type {
    AccessRequest,
    AdminFlag,
    AdminQualityRequest,
    AdminQueue,
    Discover4kRequest,
  } from '../lib/types';

  let queue = $state<AdminQueue | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);

  /** Row keys ("flag:12", "quality:4") with a request in flight. */
  let busy = $state<string[]>([]);
  /** The flag whose confirm dialog is open, if any. */
  let confirming = $state<AdminFlag | null>(null);
  /** The row key whose deny-note field is expanded, if any. */
  let denying = $state<string | null>(null);
  let note = $state('');
  let cancelButton = $state<HTMLButtonElement | null>(null);

  /**
   * Read the queue.
   *
   * `quiet` skips the loading placeholder, so a refresh after an action
   * updates the board in place instead of blanking a screen the owner is
   * still looking at. A *failed* quiet refresh keeps the last good board and
   * only toasts: the owner just approved something, and replacing the queue
   * with an error panel would hide which rows are left.
   */
  const load = async (quiet = false): Promise<void> => {
    if (!quiet) loading = true;
    error = null;
    try {
      queue = await api.get<AdminQueue>('/api/admin/queue');
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return;
      const message = err instanceof ApiError ? err.message : unreachable();
      if (quiet && queue) toasts.push(`Couldn't refresh the queue — ${message}`, 'warn');
      else error = message;
    } finally {
      loading = false;
    }
  };

  onMount(() => {
    void load();
  });

  const isBusy = (key: string): boolean => busy.includes(key);

  /**
   * Run one queue-mutating call, then re-read the queue.
   *
   * The refetch happens on failure as well as success, and that is the
   * important half: a 502 leaves the row in `approved`/`error` with the
   * upstream failure recorded on it, and only a fresh queue carries that
   * text. Without the refetch the owner would see a toast and an unchanged
   * row, with no way to tell whether anything happened.
   */
  const run = async (key: string, call: () => Promise<unknown>, success: string): Promise<void> => {
    if (isBusy(key)) return;
    busy = [...busy, key];
    try {
      await call();
      toasts.push(success);
    } catch (err) {
      toasts.push(
        err instanceof ApiError ? err.message : unreachable(),
        'warn',
      );
    } finally {
      busy = busy.filter((k) => k !== key);
      await load(true);
    }
  };

  /* ------------------------------------------------------------ deletions */

  const flagKey = (flag: AdminFlag): string => `flag:${flag.id}`;

  const approveFlag = (flag: AdminFlag): Promise<void> =>
    run(
      flagKey(flag),
      () => api.post(`/api/admin/flags/${flag.id}/approve`),
      `Deleted ${flag.title} — ${formatBytes(flag.size_bytes)} back`,
    );

  const denyFlag = async (flag: AdminFlag): Promise<void> => {
    const text = note.trim() || null;
    closeDeny();
    await run(
      flagKey(flag),
      () => api.post(`/api/admin/flags/${flag.id}/deny`, { note: text }),
      `Keeping ${flag.title}`,
    );
  };

  /** What is actually being removed: a film, a whole show, or one season. */
  const scope = (flag: AdminFlag): string => {
    if (flag.media_type === 'movie') return 'Movie';
    return flag.season_number === null ? 'Whole series' : `Season ${flag.season_number}`;
  };

  /**
   * A flag sitting in `approved` was resolved but never executed — the arr
   * call failed, or the request died mid-flight. Either way it is unfinished
   * work, not a decision waiting to be made.
   */
  const unfinished = (flag: AdminFlag): boolean => flag.state === 'approved';

  const faultOf = (flag: AdminFlag): string | null =>
    flag.error ?? (unfinished(flag) ? 'Approved, but the deletion never finished.' : null);

  /* ------------------------------------------------------ quality requests */

  const qualityKey = (req: AdminQualityRequest): string => `quality:${req.id}`;

  const approveQuality = (req: AdminQualityRequest): Promise<void> =>
    run(
      qualityKey(req),
      () => api.post(`/api/admin/quality/${req.id}/approve`),
      `Searching for a ${req.requested_quality} copy of ${req.title}`,
    );

  const denyQuality = async (req: AdminQualityRequest): Promise<void> => {
    const text = note.trim() || null;
    closeDeny();
    await run(
      qualityKey(req),
      () => api.post(`/api/admin/quality/${req.id}/deny`, { note: text }),
      `Turned down ${req.title}`,
    );
  };

  const qualityScope = (req: AdminQualityRequest): string => {
    if (req.media_type === 'movie') return 'Movie';
    return req.season_number === null ? 'Whole series' : `Season ${req.season_number}`;
  };

  /* ----------------------------------------------------- discover 4K asks */

  const discover4kKey = (req: Discover4kRequest): string => `discover4k:${req.id}`;

  const approveDiscover4k = (req: Discover4kRequest): Promise<void> =>
    run(
      discover4kKey(req),
      () => api.post(`/api/admin/discover-4k/${req.id}/approve`),
      `Filed ${req.title} in 4K`,
    );

  /**
   * Denying is not a refusal to get the thing — the server files it at 1080p
   * on the way out. The copy has to say so, or the owner will think they just
   * left someone with nothing.
   */
  const denyDiscover4k = async (req: Discover4kRequest): Promise<void> => {
    const text = note.trim() || null;
    closeDeny();
    await run(
      discover4kKey(req),
      () => api.post(`/api/admin/discover-4k/${req.id}/deny`, { note: text }),
      `Grabbing ${req.title} in 1080p instead`,
    );
  };

  /**
   * The stored season pick, or none if the column is unreadable.
   *
   * The server writes a `json.dumps` of a validated `list[int]`, so this is
   * well-formed today. It is parsed defensively anyway because it is parsed
   * *during render*: a throw here would blank the whole Approvals screen —
   * deletions and access requests included — over one malformed row.
   */
  const parseSeasons = (seasonsJson: string | null): number[] => {
    if (!seasonsJson) return [];
    try {
      const parsed: unknown = JSON.parse(seasonsJson);
      return Array.isArray(parsed) ? parsed.filter((n) => typeof n === 'number') : [];
    } catch {
      return [];
    }
  };

  /** What was actually asked for: a film, a whole show, or specific seasons. */
  const discover4kScope = (req: Discover4kRequest): string => {
    if (req.media_type === 'movie') return 'Film';
    const seasons = parseSeasons(req.seasons_json);
    if (seasons.length === 0) return 'Whole series';
    return seasons.length === 1 ? `Season ${seasons[0]}` : `Seasons ${seasons.join(', ')}`;
  };

  /* -------------------------------------------------------- access requests */

  const accessKey = (req: AccessRequest): string => `access:${req.id}`;

  /**
   * Approving reaches *outside* the homelab: it calls plex.tv to share every
   * library with that email. There is no undo button for it here — taking it
   * back means un-sharing in Plex — so the copy says what actually happened
   * rather than "approved".
   */
  const approveAccess = (req: AccessRequest): Promise<void> =>
    run(
      accessKey(req),
      () => api.post(`/api/admin/access-requests/${req.id}/approve`),
      `Shared — ${req.name} is in`,
    );

  const denyAccess = async (req: AccessRequest): Promise<void> => {
    const text = note.trim() || null;
    closeDeny();
    await run(
      accessKey(req),
      () => api.post(`/api/admin/access-requests/${req.id}/deny`, { note: text }),
      `Turned down ${req.name}`,
    );
  };

  /* --------------------------------------------------------------- shared */

  const openDeny = (key: string): void => {
    note = '';
    denying = key;
  };

  const closeDeny = (): void => {
    denying = null;
    note = '';
  };

  const confirmDelete = async (): Promise<void> => {
    const flag = confirming;
    if (!flag) return;
    confirming = null;
    await approveFlag(flag);
  };

  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== 'Escape') return;
    if (confirming) confirming = null;
    else if (denying) closeDeny();
  };

  const waiting = $derived(
    (queue?.deletions.length ?? 0) +
      (queue?.quality.length ?? 0) +
      (queue?.access.length ?? 0) +
      (queue?.discover_4k.length ?? 0),
  );

  // Send focus to the safe button, not the destructive one. A confirm dialog
  // that opens with "Delete from disk" under the cursor of a stray Enter is
  // a trap, and this is the one dialog in the app where that matters.
  $effect(() => {
    if (confirming) cancelButton?.focus();
  });
</script>

<svelte:window onkeydown={onKeydown} />

<section class="view">
  <ViewHead eyebrow="Yours to decide" title="Approvals">
    {#snippet aside()}
      {#if waiting > 0}
        <span class="count mono">{waiting} waiting</span>
      {/if}
    {/snippet}
  </ViewHead>

  {#if loading}
    <Placeholder tone="loading" message="Opening the queue…" />
  {:else if error}
    <Placeholder tone="error" message={error} onretry={() => load()} />
  {:else if queue}
    {#if waiting === 0}
      <Placeholder
        tone="empty"
        message="Nothing needs you. Deletion flags arrive here once their 14-day veto window closes, 4K requests the moment someone asks for one, and access requests when someone new knocks."
      />
    {:else}
      <!-- ------------------------------------------------------ deletions -->
      <section class="block">
        <header class="block-head">
          <p class="eyebrow warm">Deletions awaiting you</p>
          <span class="tally mono">{queue.deletions.length}</span>
        </header>

        {#if queue.deletions.length === 0}
          <p class="block-empty">
            Nothing to delete. Flags only reach you after everyone has had 14 days to say no.
          </p>
        {:else}
          <ul class="rows">
            {#each queue.deletions as flag (flag.id)}
              {@const key = flagKey(flag)}
              {@const fault = faultOf(flag)}
              <li class="row panel warm" class:faulted={fault !== null}>
                <div class="row-main">
                  <div class="detail">
                    <p class="title">{flag.title}</p>

                    <p class="meta">
                      <span class="chip mono">{scope(flag)}</span>
                      {flag.flagged_by_name} · flagged {timeAgo(flag.flagged_at)}
                    </p>

                    {#if flag.reason}
                      <p class="reason">“{flag.reason}”</p>
                    {/if}
                  </div>

                  <p class="figure mono">
                    <span class="figure-num">{formatBytes(flag.size_bytes)}</span>
                    <span class="figure-unit">comes back</span>
                  </p>
                </div>

                {#if fault}
                  <!-- Owner-only: the unedited upstream text, because the
                       point of it is that nobody rewrote it. -->
                  <p class="fault mono" role="status">{fault}</p>
                {/if}

                {#if denying === key}
                  <div class="deny-form">
                    <label class="field">
                      <span class="eyebrow">Tell them why (optional)</span>
                      <input
                        class="input"
                        type="text"
                        maxlength="200"
                        placeholder="Still want this around…"
                        bind:value={note}
                        disabled={isBusy(key)}
                      />
                    </label>

                    <div class="deny-acts">
                      <button class="act ghost" disabled={isBusy(key)} onclick={closeDeny}>
                        Cancel
                      </button>
                      <button class="act cold" disabled={isBusy(key)} onclick={() => denyFlag(flag)}>
                        Keep it
                      </button>
                    </div>
                  </div>
                {:else}
                  <div class="acts">
                    {#if fault}
                      <button class="act retry" disabled={isBusy(key)} onclick={() => approveFlag(flag)}>
                        {isBusy(key) ? 'Retrying…' : 'Retry deletion'}
                      </button>
                    {:else}
                      <button class="act ghost" disabled={isBusy(key)} onclick={() => openDeny(key)}>
                        Deny
                      </button>
                      <button
                        class="act danger"
                        disabled={isBusy(key)}
                        onclick={() => (confirming = flag)}
                      >
                        {isBusy(key) ? 'Deleting…' : 'Approve deletion'}
                      </button>
                    {/if}
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- ------------------------------------------------ quality requests -->
      <section class="block">
        <header class="block-head">
          <p class="eyebrow">Quality requests</p>
          <span class="tally mono">{queue.quality.length}</span>
        </header>

        {#if queue.quality.length === 0}
          <p class="block-empty">
            No one is asking for a better copy. 1080p upgrades start on their own — only 4K needs
            you.
          </p>
        {:else}
          <ul class="rows">
            {#each queue.quality as req (req.id)}
              {@const key = qualityKey(req)}
              <li class="row panel" class:faulted={req.error !== null}>
                <div class="row-main">
                  <div class="detail">
                    <p class="title">{req.title}</p>

                    <p class="meta">
                      <span class="chip mono">{qualityScope(req)}</span>
                      {req.requested_by_name} · asked {timeAgo(req.created_at)}
                    </p>
                  </div>

                  <p class="tier mono">
                    <span class="tier-want">{req.requested_quality}</span>
                    {#if req.current_quality}
                      <span class="tier-from">from {req.current_quality}</span>
                    {/if}
                  </p>
                </div>

                {#if req.error}
                  <p class="fault mono" role="status">{req.error}</p>
                {/if}

                {#if denying === key}
                  <div class="deny-form">
                    <label class="field">
                      <span class="eyebrow">Tell them why (optional)</span>
                      <input
                        class="input"
                        type="text"
                        maxlength="200"
                        placeholder="No space for 4K right now…"
                        bind:value={note}
                        disabled={isBusy(key)}
                      />
                    </label>

                    <div class="deny-acts">
                      <button class="act ghost" disabled={isBusy(key)} onclick={closeDeny}>
                        Cancel
                      </button>
                      <button
                        class="act cold"
                        disabled={isBusy(key)}
                        onclick={() => denyQuality(req)}
                      >
                        Turn it down
                      </button>
                    </div>
                  </div>
                {:else}
                  <div class="acts">
                    {#if req.error}
                      <button
                        class="act retry"
                        disabled={isBusy(key)}
                        onclick={() => approveQuality(req)}
                      >
                        {isBusy(key) ? 'Retrying…' : 'Retry search'}
                      </button>
                    {:else}
                      <button class="act ghost" disabled={isBusy(key)} onclick={() => openDeny(key)}>
                        Deny
                      </button>
                      <button
                        class="act cold"
                        disabled={isBusy(key)}
                        onclick={() => approveQuality(req)}
                      >
                        {isBusy(key) ? 'Starting…' : `Approve ${req.requested_quality}`}
                      </button>
                    {/if}
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- ------------------------------------------------ discover 4K asks -->
      <section class="block">
        <header class="block-head">
          <p class="eyebrow">4K asks from Discover</p>
          <span class="tally mono">{queue.discover_4k.length}</span>
        </header>

        {#if queue.discover_4k.length === 0}
          <p class="block-empty">
            Nobody is asking for 4K. Requests for anything the server doesn&rsquo;t have yet come
            through at 1080p on their own &mdash; only 4K stops here first.
          </p>
        {:else}
          <ul class="rows">
            {#each queue.discover_4k as req (req.id)}
              {@const key = discover4kKey(req)}
              <li class="row panel">
                <div class="row-main">
                  <div class="detail">
                    <p class="title">{req.title}</p>

                    <p class="meta">
                      <span class="mono">{discover4kScope(req)}</span>
                      &middot; {req.requested_by_name} asked {timeAgo(req.created_at)}
                    </p>
                  </div>
                </div>

                {#if denying === key}
                  <div class="deny-form">
                    <label class="field">
                      <span class="eyebrow">Tell them why (optional)</span>
                      <input
                        class="input"
                        type="text"
                        maxlength="200"
                        placeholder="No space for 4K right now…"
                        bind:value={note}
                        disabled={isBusy(key)}
                      />
                    </label>

                    <div class="deny-acts">
                      <button class="act ghost" disabled={isBusy(key)} onclick={closeDeny}>
                        Cancel
                      </button>
                      <button
                        class="act cold"
                        disabled={isBusy(key)}
                        onclick={() => denyDiscover4k(req)}
                      >
                        Send it at 1080p
                      </button>
                    </div>
                  </div>
                {:else}
                  <div class="acts">
                    <button class="act ghost" disabled={isBusy(key)} onclick={() => openDeny(key)}>
                      Not at 4K
                    </button>
                    <button
                      class="act cold"
                      disabled={isBusy(key)}
                      onclick={() => approveDiscover4k(req)}
                    >
                      {isBusy(key) ? 'Filing…' : 'Approve 4K'}
                    </button>
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- -------------------------------------------------- access requests -->
      <section class="block">
        <header class="block-head">
          <p class="eyebrow">Access requests</p>
          <span class="tally mono">{queue.access.length}</span>
        </header>

        {#if queue.access.length === 0}
          <p class="block-empty">
            Nobody is knocking. Anyone who signs in with a Plex account you haven&rsquo;t shared with
            lands here.
          </p>
        {:else}
          <ul class="rows">
            {#each queue.access as req (req.id)}
              {@const key = accessKey(req)}
              <li class="row panel">
                <div class="row-main">
                  <div class="detail">
                    <p class="title">{req.name}</p>

                    <p class="meta">
                      <span class="email mono">{req.email}</span>
                      asked {timeAgo(req.created_at)}
                    </p>
                  </div>
                </div>

                {#if denying === key}
                  <div class="deny-form">
                    <label class="field">
                      <span class="eyebrow">Tell them why (optional)</span>
                      <input
                        class="input"
                        type="text"
                        maxlength="200"
                        placeholder="Not right now…"
                        bind:value={note}
                        disabled={isBusy(key)}
                      />
                    </label>

                    <div class="deny-acts">
                      <button class="act ghost" disabled={isBusy(key)} onclick={closeDeny}>
                        Cancel
                      </button>
                      <button
                        class="act cold"
                        disabled={isBusy(key)}
                        onclick={() => denyAccess(req)}
                      >
                        Turn it down
                      </button>
                    </div>
                  </div>
                {:else}
                  <div class="acts">
                    <button class="act ghost" disabled={isBusy(key)} onclick={() => openDeny(key)}>
                      Deny
                    </button>
                    <button class="act cold" disabled={isBusy(key)} onclick={() => approveAccess(req)}>
                      {isBusy(key) ? 'Sharing…' : 'Share the library'}
                    </button>
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    {/if}

    <!-- ------------------------------------------------- waiting on plex -->
    {#if queue.waiting_on_plex.length > 0}
      <section class="block">
        <header class="block-head">
          <p class="eyebrow">Waiting on Plex</p>
          <span class="tally mono">{queue.waiting_on_plex.length}</span>
        </header>

        <p class="block-empty">
          Approved, and signed in — but they haven&rsquo;t accepted the Plex invite yet, so they
          can browse and can&rsquo;t request anything. Only they can accept it.
        </p>

        <ul class="rows">
          {#each queue.waiting_on_plex as person (person.plex_account_id)}
            <li class="row panel">
              <div class="row-main">
                <div class="detail">
                  <p class="title">{person.name}</p>
                  <p class="meta">
                    <span class="email mono">{person.email}</span>
                    invited {timeAgo(new Date(person.invited_at * 1000).toISOString())}
                  </p>
                </div>
              </div>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}
</section>

<!-- ------------------------------------------------------- confirm dialog -->

{#if confirming}
  <div class="layer">
    <button class="scrim" aria-label="Cancel" onclick={() => (confirming = null)}></button>

    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
      <p class="eyebrow warm">This cannot be undone</p>

      <h3 id="confirm-title">Delete {confirming.title}?</h3>

      <p class="dialog-body">
        The files come off the disk as soon as you say yes — {scope(confirming).toLowerCase()},
        <span class="mono">{formatBytes(confirming.size_bytes)}</span>. {branding.appName} has no
        copy and no way back; it would have to be downloaded again.
      </p>

      <p class="dialog-meta">
        {confirming.flagged_by_name} flagged this {timeAgo(confirming.flagged_at)} and nobody vetoed
        it.
      </p>

      <div class="dialog-acts">
        <!-- "Cancel", not "Keep it": this only dismisses the dialog. The row's
             own Deny button is what actually records a decision, and reusing
             its wording here would let a dismissal read as a denial. -->
        <button class="act ghost" bind:this={cancelButton} onclick={() => (confirming = null)}>
          Cancel
        </button>
        <button class="act danger solid" onclick={confirmDelete}>Delete from disk</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .view {
    animation: rise var(--dur-slow) var(--ease) both;
  }

  .count {
    padding: 2px var(--sp-3);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.08em;
  }

  /* ----------------------------------------------------------- sections */

  .block + .block {
    margin-top: var(--sp-7);
  }

  .block-head {
    display: flex;
    align-items: baseline;
    gap: var(--sp-3);
    margin-bottom: var(--sp-3);
  }

  /* The section label itself carries the grading, so the queue announces
     what kind of decision it holds before any row is read. */
  .eyebrow.warm {
    color: var(--ember);
  }

  .tally {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
  }

  .block-empty {
    padding: var(--sp-4) 0;
    color: var(--rune-dim);
    font-size: var(--fs-sm);
    text-wrap: pretty;
  }

  .rows {
    display: flex;
    flex-direction: column;
    gap: var(--sp-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* --------------------------------------------------------------- row */

  .row {
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
    padding: var(--sp-4) var(--sp-5);
  }

  /* Warm edge: everything in this list ends in files being destroyed. */
  .row.warm {
    border-color: rgb(var(--rose-rgb) / 20%);
  }

  .row.faulted {
    border-color: rgb(var(--rose-rgb) / 38%);
    background:
      linear-gradient(180deg, rgb(242 112 139 / 6%), transparent 44%),
      var(--basin);
  }

  .row-main {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--sp-4);
  }

  .detail {
    min-width: 0;
  }

  .title {
    font-size: var(--fs-md);
    text-wrap: balance;
  }

  .meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--sp-2);
    margin-top: var(--sp-1);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .chip {
    padding: 1px var(--sp-2);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    font-size: var(--fs-micro);
    letter-spacing: 0.04em;
  }

  /* Long addresses must wrap rather than push the row wide on a phone. */
  .email {
    overflow-wrap: anywhere;
  }

  .reason {
    margin-top: var(--sp-2);
    color: var(--rune);
    font-size: var(--fs-sm);
    font-style: italic;
    text-wrap: pretty;
  }

  /* The size is the consequence, so it is the one lit figure in the row. */
  .figure {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    flex: none;
    line-height: 1.2;
    text-align: right;
  }

  .figure-num {
    color: var(--ember);
    font-size: var(--fs-md);
  }

  .figure-unit {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.08em;
  }

  .tier {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    flex: none;
    line-height: 1.2;
    text-align: right;
  }

  .tier-want {
    color: var(--memory);
    font-size: var(--fs-md);
  }

  .tier-from {
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
  }

  /* ------------------------------------------------------------- fault */

  .fault {
    padding: var(--sp-3) var(--sp-4);
    border: 1px solid rgb(var(--rose-rgb) / 26%);
    border-radius: var(--r-md);
    background: rgb(var(--rose-rgb) / 7%);
    color: #ffd3c2;
    font-size: var(--fs-xs);
    line-height: 1.5;
    /* Upstream exception text — unbroken URLs and paths must not widen the row. */
    overflow-wrap: anywhere;
  }

  /* ------------------------------------------------------------- acts */

  .acts,
  .deny-acts {
    display: flex;
    justify-content: flex-end;
    gap: var(--sp-3);
  }

  .act {
    padding: var(--sp-2) var(--sp-4);
    border: 1px solid var(--edge-hi);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-sm);
    transition:
      color var(--dur-fast) var(--ease),
      border-color var(--dur-fast) var(--ease),
      background-color var(--dur-fast) var(--ease);
  }

  .act.ghost:hover:not(:disabled) {
    color: var(--vapor);
    border-color: var(--edge-glow);
  }

  .act.cold {
    border-color: rgb(var(--memory-rgb) / 40%);
    color: var(--memory);
  }

  .act.cold:hover:not(:disabled) {
    background: var(--memory-wash);
    border-color: rgb(var(--memory-rgb) / 70%);
  }

  /* Outlined, never filled: the destructive act should not be the brightest
     thing on the row. Only the dialog's final button is solid. */
  .act.danger {
    border-color: rgb(var(--rose-rgb) / 45%);
    color: var(--ember);
  }

  .act.danger:hover:not(:disabled) {
    background: rgb(var(--rose-rgb) / 14%);
  }

  .act.danger.solid {
    background: rgb(var(--rose-rgb) / 18%);
    border-color: rgb(var(--rose-rgb) / 60%);
  }

  .act.danger.solid:hover {
    background: rgb(var(--rose-rgb) / 30%);
  }

  .act.retry {
    border-color: rgb(242 167 95 / 45%);
    color: var(--ember);
  }

  .act.retry:hover:not(:disabled) {
    background: rgb(242 167 95 / 14%);
  }

  .act:disabled {
    opacity: 0.5;
    cursor: default;
  }

  /* --------------------------------------------------------- deny form */

  .deny-form {
    display: flex;
    flex-direction: column;
    gap: var(--sp-3);
    animation: rise var(--dur) var(--ease) both;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
  }

  .input {
    padding: var(--sp-3) var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-md);
    background: var(--ink-raised);
    color: var(--vapor);
    font: inherit;
    /* 16px floor, or focusing this field zooms the app — see --fs-field. */
    font-size: var(--fs-field);
    transition: border-color var(--dur-fast) var(--ease);
  }

  .input::placeholder {
    color: var(--rune-dim);
  }

  .input:focus {
    border-color: var(--edge-glow);
    outline: none;
  }

  /* ------------------------------------------------------------ dialog */

  .layer {
    position: fixed;
    inset: 0;
    z-index: 60;
    display: grid;
    place-items: center;
    padding: var(--sp-5);
  }

  .scrim {
    position: absolute;
    inset: 0;
    background: rgb(3 5 8 / 74%);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    cursor: default;
    animation: fade var(--dur) var(--ease) both;
  }

  @keyframes fade {
    from {
      opacity: 0;
    }
  }

  /* Centred, not a bottom sheet: the sheet is the members' surface for
     proposing things. This is a different act and gets a different shape. */
  .dialog {
    position: relative;
    width: min(28rem, 100%);
    padding: var(--sp-5);
    border: 1px solid rgb(var(--rose-rgb) / 34%);
    border-radius: var(--r-lg);
    background:
      linear-gradient(180deg, rgb(242 112 139 / 8%), transparent 42%),
      var(--basin);
    box-shadow: var(--shadow-lift);
    animation: lift var(--dur) var(--ease) both;
  }

  @keyframes lift {
    from {
      opacity: 0;
      transform: translateY(16px) scale(0.98);
    }
  }

  h3 {
    margin-top: var(--sp-2);
    font-size: var(--fs-lg);
    text-wrap: balance;
  }

  .dialog-body {
    margin-top: var(--sp-4);
    color: var(--rune);
    font-size: var(--fs-sm);
    text-wrap: pretty;
  }

  .dialog-meta {
    margin-top: var(--sp-3);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
    text-wrap: pretty;
  }

  .dialog-acts {
    display: flex;
    justify-content: flex-end;
    gap: var(--sp-3);
    margin-top: var(--sp-5);
  }

  /* ------------------------------------------------------------ narrow */

  @media (width < 34rem) {
    .row-main {
      flex-direction: column;
      gap: var(--sp-2);
    }

    .figure,
    .tier {
      flex-direction: row;
      align-items: baseline;
      gap: var(--sp-2);
      text-align: left;
    }

    .acts,
    .deny-acts,
    .dialog-acts {
      flex-direction: column-reverse;
    }

    .act {
      width: 100%;
      padding-block: var(--sp-3);
    }
  }
</style>
