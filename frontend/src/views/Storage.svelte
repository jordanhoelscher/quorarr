<script lang="ts">
  /**
   * How much room is left, and what is using it.
   *
   * Leads with *free* space rather than used: this view exists so a friend can
   * answer "is there room for what I want to ask for", and the answer to that
   * is a single number. The library split below it answers the follow-up —
   * "what is actually in there" — as one honest two-segment bar.
   */
  import { onMount } from 'svelte';
  import { unreachable } from '../lib/branding.svelte';

  import Placeholder from '../lib/Placeholder.svelte';
  import ViewHead from '../lib/ViewHead.svelte';
  import { ApiError, api } from '../lib/api';
  import { formatBytes, staleLabel } from '../lib/format';
  import type { StorageSummary } from '../lib/types';

  let data = $state<StorageSummary | null>(null);
  let error = $state<string | null>(null);
  let loading = $state(true);

  const load = async (): Promise<void> => {
    loading = true;
    error = null;
    try {
      data = await api.get<StorageSummary>('/api/storage');
    } catch (err) {
      // A 401 already dropped us to the login screen; anything else is ours.
      if (!(err instanceof ApiError && err.status === 401)) {
        error = err instanceof ApiError ? err.message : unreachable();
      }
    } finally {
      loading = false;
    }
  };

  onMount(load);

  /** Split "1.5 TB" so the figure can be set large and the unit small. */
  const parts = (bytes: number): [string, string] => {
    const [amount, unit] = formatBytes(bytes).split(' ');
    return [amount, unit];
  };

  const usedPct = $derived(data && data.total_bytes > 0 ? (data.used_bytes / data.total_bytes) * 100 : 0);
  const libraryBytes = $derived(data ? data.movies_bytes + data.tv_bytes : 0);
  /**
   * Only meaningful once there is something in the library. An empty library
   * renders an empty trough rather than a 50/50 split — a bar reading "half
   * movies, half TV" over 0 B is a fabricated measurement, and this view's
   * whole job is to be trusted about sizes.
   */
  const moviesPct = $derived(libraryBytes > 0 && data ? (data.movies_bytes / libraryBytes) * 100 : 0);
</script>

<section class="view">
  <ViewHead eyebrow="Disk" title="Storage">
    {#snippet aside()}
      {#if data?.stale_seconds !== undefined}
        <span class="stale mono">{staleLabel(data.stale_seconds)}</span>
      {/if}
    {/snippet}
  </ViewHead>

  {#if loading && !data}
    <Placeholder tone="loading" message="Reading the disk…" />
  {:else if error}
    <Placeholder tone="error" message={error} onretry={load} />
  {:else if data}
    {@const [freeAmount, freeUnit] = parts(data.free_bytes)}

    <div class="grid">
      <div class="panel headroom">
        <p class="eyebrow">Free space</p>

        <p class="figure">
          <span class="amount mono">{freeAmount}</span>
          <span class="unit mono">{freeUnit}</span>
        </p>

        <div
          class="trough"
          role="img"
          aria-label="{formatBytes(data.used_bytes)} used of {formatBytes(data.total_bytes)}"
        >
          <span class="fill" style="width: {usedPct.toFixed(2)}%"></span>
        </div>

        <p class="under mono">
          {formatBytes(data.used_bytes)} used of {formatBytes(data.total_bytes)}
          <span class="pct">· {Math.round(usedPct)}% full</span>
        </p>
      </div>

      <div class="panel split">
        <p class="eyebrow">In the basin</p>

        <div
          class="trough"
          role="img"
          aria-label={libraryBytes > 0
            ? `Movies ${formatBytes(data.movies_bytes)}, TV ${formatBytes(data.tv_bytes)}`
            : 'Nothing in the library yet'}
        >
          <!-- No segments at all on an empty library: a filled bar here would
               be inventing a ratio out of two zeroes. -->
          {#if libraryBytes > 0}
            <span class="seg movies" style="width: {moviesPct.toFixed(2)}%"></span>
            <span class="seg tv" style="width: {(100 - moviesPct).toFixed(2)}%"></span>
          {/if}
        </div>

        <dl class="legend">
          <div class="row">
            <dt><span class="key movies" aria-hidden="true"></span>Movies</dt>
            <dd class="count mono">{data.movie_count} films</dd>
            <dd class="size mono">{formatBytes(data.movies_bytes)}</dd>
          </div>

          <div class="row">
            <dt><span class="key tv" aria-hidden="true"></span>TV</dt>
            <dd class="count mono">{data.series_count} series</dd>
            <dd class="size mono">{formatBytes(data.tv_bytes)}</dd>
          </div>
        </dl>

        <p class="under mono">
          {#if libraryBytes > 0}
            {formatBytes(libraryBytes)} of library on disk
          {:else}
            Nothing on disk yet — the first request will show up here
          {/if}
        </p>
      </div>
    </div>
  {/if}
</section>

<style>
  .view {
    animation: rise var(--dur-slow) var(--ease) both;
  }

  .stale {
    padding: 2px var(--sp-3);
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    color: var(--rune-dim);
    font-size: var(--fs-micro);
    letter-spacing: 0.06em;
    white-space: nowrap;
  }

  .grid {
    display: grid;
    gap: var(--sp-4);
  }

  @media (width >= 44rem) {
    .grid {
      grid-template-columns: 1fr 1fr;
      align-items: start;
    }
  }

  /* -------------------------------------------------------- headroom */

  .figure {
    display: flex;
    align-items: baseline;
    gap: var(--sp-2);
    margin: var(--sp-3) 0 var(--sp-5);
  }

  .amount {
    font-size: var(--fs-2xl);
    font-weight: 300;
    line-height: 1;
    letter-spacing: -0.03em;
    color: var(--vapor);
    text-shadow: 0 0 34px rgb(var(--memory-rgb) / 28%);
  }

  .unit {
    font-size: var(--fs-md);
    letter-spacing: 0.08em;
    color: var(--rune-dim);
    text-transform: uppercase;
  }

  /* --------------------------------------------------------- meters */

  .trough {
    display: flex;
    overflow: hidden;
    height: 10px;
    border: 1px solid var(--edge);
    border-radius: var(--r-full);
    background: var(--ink-raised);
    box-shadow: inset 0 1px 3px rgb(0 0 0 / 60%);
  }

  .fill {
    height: 100%;
    border-radius: var(--r-full);
    background: linear-gradient(90deg, var(--memory-deep), var(--memory));
    box-shadow: 0 0 14px -1px rgb(var(--memory-rgb) / 60%);
    /* Fills from empty on first paint, so the meter reads as a measurement. */
    animation: pour var(--dur-slow) var(--ease) both;
  }

  @keyframes pour {
    from {
      transform: scaleX(0);
      transform-origin: left;
    }
  }

  .seg {
    height: 100%;
  }

  .seg.movies {
    background: linear-gradient(90deg, var(--memory-deep), var(--memory));
  }

  /* Same accent, sunk: two readings of one substance, not two colours. */
  .seg.tv {
    background: var(--memory-deep);
    opacity: 0.45;
  }

  .under {
    margin-top: var(--sp-3);
    color: var(--rune-dim);
    font-size: var(--fs-xs);
  }

  .pct {
    color: var(--rune-dim);
    opacity: 0.7;
  }

  /* --------------------------------------------------------- legend */

  .legend {
    margin: var(--sp-4) 0 0;
  }

  .row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    align-items: baseline;
    gap: var(--sp-3);
    padding-block: var(--sp-2);
    border-top: 1px solid var(--edge);
  }

  dt {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    color: var(--vapor);
    font-size: var(--fs-sm);
  }

  dd {
    margin: 0;
    font-size: var(--fs-xs);
  }

  .count {
    color: var(--rune-dim);
  }

  .size {
    color: var(--rune);
    min-width: 4.5rem;
    text-align: right;
  }

  .key {
    width: 8px;
    height: 8px;
    border-radius: 2px;
  }

  .key.movies {
    background: var(--memory);
    box-shadow: var(--glow-memory);
  }

  .key.tv {
    background: var(--memory-deep);
    opacity: 0.55;
  }

  .split .trough {
    margin-top: var(--sp-4);
  }
</style>
