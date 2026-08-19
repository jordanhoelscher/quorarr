<script lang="ts">
  /**
   * A poster thumbnail with a stone fallback.
   *
   * Posters are third-party remote URLs straight from Radarr/Sonarr metadata
   * (TMDB, fanart.tv), so any one of them can 404 or be blocked. On failure
   * this collapses to a carved-stone placeholder of the same size rather than
   * leaving a broken-image glyph in the row. `loading="lazy"` matters: the
   * library list is thousands of rows deep.
   */
  interface Props {
    src: string | null;
    /** Empty for decorative use next to a visible title. */
    alt?: string;
    /**
     * Row thumbs, the detail overlay's header, a Discover shelf tile, or the
     * round headshot a person wears in the search dropdown.
     */
    size?: 'row' | 'lead' | 'tile' | 'face';
  }

  const { src, alt = '', size = 'row' }: Props = $props();

  let failed = $state(false);
</script>

{#if src && !failed}
  <img
    class="poster"
    class:lead={size === 'lead'}
    class:tile={size === 'tile'}
    class:face={size === 'face'}
    {src}
    {alt}
    loading="lazy"
    decoding="async"
    referrerpolicy="no-referrer"
    onerror={() => (failed = true)}
  />
{:else}
  <span
    class="poster empty"
    class:lead={size === 'lead'}
    class:tile={size === 'tile'}
    class:face={size === 'face'}
    aria-hidden="true"
  ></span>
{/if}

<style>
  .poster {
    flex: none;
    width: 2.5rem;
    height: 3.75rem;
    border: 1px solid var(--edge);
    border-radius: var(--r-sm);
    object-fit: cover;
    background: var(--basin-hi);
  }

  .poster.lead {
    width: 4.5rem;
    height: 6.75rem;
    border-radius: var(--r-md);
  }

  /*
   * Fills whatever cell the shelf or grid gives it. `aspect-ratio` rather than
   * a fixed height so a rail of posters keeps one baseline no matter how wide
   * the viewport gets — a stray 3:4 poster would otherwise notch the row.
   */
  .poster.tile {
    width: 100%;
    height: auto;
    aspect-ratio: 2 / 3;
    border-radius: var(--r-md);
  }

  /*
   * A person, not a poster: square and round, so a dropdown row reads as
   * "who" at a glance rather than as a title whose artwork failed to load.
   */
  .poster.face {
    width: 2.5rem;
    height: 2.5rem;
    border-radius: var(--r-full);
    object-position: top center;
  }

  /* Carved stone: a lit top edge over a darker face, same as .panel. */
  .empty {
    display: block;
    background:
      linear-gradient(180deg, rgb(255 255 255 / 5%), transparent 45%),
      var(--basin-hi);
  }
</style>
