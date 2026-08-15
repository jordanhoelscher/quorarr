<script lang="ts">
  /**
   * The one place a friend can change something.
   *
   * A bottom sheet with two acts — ask for a better copy, or propose losing
   * this one — that expand in place rather than opening a second modal. The
   * server's answer always arrives as a toast, because committing either act
   * closes the sheet.
   *
   * The parent renders this only while a target exists and clears the target
   * in `onclose`, so every open is a fresh instance with `mode` back at the
   * menu. That is deliberate: no `$effect` reset to get wrong.
   */
  import { ApiError, api } from './api';
  import { SENT_FOR_APPROVAL, branding, unreachable } from './branding.svelte';
  import { formatBytes } from './format';
  import { toasts } from './toast.svelte';
  import type { ActionTarget, QualityRequestResult } from './types';

  interface Props {
    target: ActionTarget;
    onclose: () => void;
  }

  const { target, onclose }: Props = $props();

  type Mode = 'menu' | 'quality' | 'delete';

  let mode = $state<Mode>('menu');
  let reason = $state('');
  let busy = $state(false);

  /** Seasons and whole series are both "series" to the backend. */
  const mediaType = $derived(target.kind === 'movie' ? 'movie' : 'series');

  const subtitle = $derived(
    target.kind === 'season'
      ? `Season ${target.seasonNumber} · ${formatBytes(target.sizeBytes)}`
      : formatBytes(target.sizeBytes),
  );

  /**
   * Offering 1080p for a file that is already 1080p or better is a request to
   * downgrade. Only movies carry a resolution; a series spans many files at
   * mixed qualities, so both options always stand there.
   */
  const canRequest1080 = $derived(
    target.kind !== 'movie' || (target.resolution ?? 0) < 1080,
  );

  /**
   * Nothing above 4K exists to go and fetch, so asking for it is a request
   * for what is already on the disk.
   *
   * `?? 0` is load-bearing in both of these: a tracked-but-not-downloaded
   * film has a null resolution, and null must read as "no file yet, anything
   * is an upgrade" rather than as a height to compare. Written as a bare
   * `>= 2160` it would hide the action for every film that hasn't downloaded.
   */
  const canRequest4K = $derived(
    target.kind !== 'movie' || (target.resolution ?? 0) < 2160,
  );

  /** With neither copy on offer, the act leads nowhere and shouldn't be shown. */
  const canRequestQuality = $derived(canRequest1080 || canRequest4K);

  /** Turn any thrown error into something a friend can act on. */
  const explain = (err: unknown): string =>
    err instanceof ApiError ? err.message : unreachable();

  const requestQuality = async (requested: '1080p' | '4K'): Promise<void> => {
    if (busy) return;
    busy = true;
    try {
      const result = await api.post<QualityRequestResult>('/api/quality-requests', {
        media_type: mediaType,
        arr_id: target.arrId,
        season_number: target.seasonNumber ?? null,
        title: target.title,
        requested,
        current_quality: target.quality ?? null,
      });

      if (result.state === 'auto_triggered') {
        toasts.push('Upgrade search started');
      } else if (result.state === 'pending_approval') {
        toasts.push(SENT_FOR_APPROVAL());
      } else {
        // A 200 carrying state "error" isn't a shape the backend documents,
        // but saying nothing would read as success.
        toasts.push('That upgrade could not be started', 'warn');
      }
      onclose();
    } catch (err) {
      toasts.push(explain(err), 'warn');
      busy = false;
    }
  };

  const flagForDeletion = async (): Promise<void> => {
    if (busy) return;
    busy = true;
    try {
      await api.post('/api/flags', {
        media_type: mediaType,
        arr_id: target.arrId,
        season_number: target.seasonNumber ?? null,
        title: target.title,
        size_bytes: target.sizeBytes,
        reason: reason.trim() || null,
      });
      toasts.push('Flagged — friends have 14 days to veto');
      onclose();
    } catch (err) {
      toasts.push(explain(err), 'warn');
      busy = false;
    }
  };
</script>

<svelte:window onkeydown={(event) => event.key === 'Escape' && onclose()} />

<div class="layer">
  <button class="scrim" aria-label="Close" onclick={onclose}></button>

  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title">
    <span class="grip" aria-hidden="true"></span>

    <header class="head">
      <h3 id="sheet-title">{target.title}</h3>
      <p class="sub mono">{subtitle}</p>
    </header>

    {#if mode === 'menu'}
      <div class="acts">
        {#if canRequestQuality}
          <button class="act" onclick={() => (mode = 'quality')}>
            <span class="act-name">Request better quality</span>
            <span class="act-note">Ask for a 1080p or 4K copy</span>
          </button>
        {/if}

        <button class="act danger" onclick={() => (mode = 'delete')}>
          <span class="act-name">Mark for deletion</span>
          <span class="act-note">Everyone gets 14 days to say no</span>
        </button>
      </div>
    {:else if mode === 'quality'}
      <div class="step">
        <p class="prompt">Which copy should the server go looking for?</p>

        <div class="choices">
          {#if canRequest1080}
            <button class="choice" disabled={busy} onclick={() => requestQuality('1080p')}>
              <span class="choice-name mono">1080p</span>
              <span class="choice-note">Starts straight away</span>
            </button>
          {/if}

          <button class="choice" disabled={busy} onclick={() => requestQuality('4K')}>
            <span class="choice-name mono">4K</span>
            <span class="choice-note">Needs {branding.ownerName}'s approval</span>
          </button>
        </div>

        {#if !canRequest1080}
          <p class="hint">This is already 1080p or better, so only 4K is worth asking for.</p>
        {/if}

        <button class="back" disabled={busy} onclick={() => (mode = 'menu')}>Back</button>
      </div>
    {:else}
      <div class="step">
        <p class="prompt danger-text">
          This proposes deleting it. Nothing is removed until the 14-day window closes and
          {branding.ownerName} approves.
        </p>

        <label class="field">
          <span class="eyebrow">Why (optional)</span>
          <input
            class="input"
            type="text"
            maxlength="200"
            placeholder="Watched it, never finished it…"
            bind:value={reason}
            disabled={busy}
          />
        </label>

        <div class="confirm-row">
          <button class="back" disabled={busy} onclick={() => (mode = 'menu')}>Back</button>
          <button class="confirm" disabled={busy} onclick={flagForDeletion}>
            {busy ? 'Flagging…' : 'Mark for deletion'}
          </button>
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .layer {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
  }

  .scrim {
    position: absolute;
    inset: 0;
    background: rgb(3 5 8 / 68%);
    backdrop-filter: blur(3px);
    -webkit-backdrop-filter: blur(3px);
    cursor: default;
    animation: fade var(--dur) var(--ease) both;
  }

  @keyframes fade {
    from {
      opacity: 0;
    }
  }

  .sheet {
    position: relative;
    width: min(30rem, 100%);
    margin-inline: auto;
    padding: var(--sp-3) var(--sp-5) calc(var(--sp-6) + env(safe-area-inset-bottom));
    border: 1px solid var(--edge-hi);
    border-bottom: 0;
    border-radius: var(--r-lg) var(--r-lg) 0 0;
    background:
      linear-gradient(180deg, rgb(255 255 255 / 4%), transparent 30%),
      var(--basin);
    box-shadow: var(--shadow-lift);
    animation: slide-up var(--dur) var(--ease) both;
  }

  @keyframes slide-up {
    from {
      opacity: 0;
      transform: translateY(28px);
    }
  }

  .grip {
    display: block;
    width: 2.25rem;
    height: 3px;
    margin: 0 auto var(--sp-4);
    border-radius: var(--r-full);
    background: var(--edge-hi);
  }

  .head {
    margin-bottom: var(--sp-5);
  }

  h3 {
    font-size: var(--fs-lg);
    text-wrap: balance;
  }

  .sub {
    margin-top: var(--sp-1);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  /* ------------------------------------------------------------ menu */

  .acts {
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
  }

  .act {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-md);
    text-align: left;
    transition:
      border-color var(--dur-fast) var(--ease),
      background-color var(--dur-fast) var(--ease);
  }

  .act:hover {
    border-color: var(--edge-glow);
    background: var(--memory-wash);
  }

  /* The only warm surface in the app, and it is the one that destroys. */
  .act.danger:hover {
    border-color: rgb(var(--rose-rgb) / 40%);
    background: rgb(var(--rose-rgb) / 7%);
  }

  .act-name {
    font-size: var(--fs-md);
  }

  .act.danger .act-name {
    color: var(--ember);
  }

  .act-note {
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  /* ------------------------------------------------------------ step */

  .step {
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
    animation: rise var(--dur) var(--ease) both;
  }

  .prompt {
    color: var(--rune);
    font-size: var(--fs-sm);
    text-wrap: pretty;
  }

  .danger-text {
    color: var(--ember);
  }

  .choices {
    display: flex;
    gap: var(--sp-3);
  }

  .choice {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-md);
    text-align: left;
    transition:
      border-color var(--dur-fast) var(--ease),
      background-color var(--dur-fast) var(--ease);
  }

  .choice:hover:not(:disabled) {
    border-color: var(--edge-glow);
    background: var(--memory-wash);
  }

  .choice-name {
    color: var(--memory);
    font-size: var(--fs-md);
  }

  .choice-note {
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .hint {
    color: var(--rune-dim);
    font-size: var(--fs-xs);
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

  .confirm-row {
    display: flex;
    gap: var(--sp-3);
  }

  .back {
    padding: var(--sp-3) var(--sp-4);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune);
    font-size: var(--fs-sm);
    align-self: flex-start;
  }

  .back:hover:not(:disabled) {
    color: var(--vapor);
    border-color: var(--edge-hi);
  }

  .confirm {
    flex: 1;
    padding: var(--sp-3) var(--sp-4);
    border: 1px solid rgb(var(--rose-rgb) / 45%);
    border-radius: var(--r-full);
    background: rgb(var(--rose-rgb) / 12%);
    color: var(--ember);
    font-size: var(--fs-sm);
    transition: background-color var(--dur-fast) var(--ease);
  }

  .confirm:hover:not(:disabled) {
    background: rgb(var(--rose-rgb) / 20%);
  }

  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
</style>
