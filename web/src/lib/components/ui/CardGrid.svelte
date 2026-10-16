<script lang="ts" module>
  export type CardGridColumns = 1 | 2 | 3 | 4 | 5;
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';
  import { masonry as masonryLayout } from '$lib/masonry';
  import { cardFill } from '$lib/cardFill';

  interface Props {
    /** The most columns the grid grows to; it steps down to one as its container narrows. */
    columns?: CardGridColumns;
    masonry?: boolean;
    /** Width over height a card may not go below, measured on one column: 1 keeps every card at least square. */
    minAspectRatio?: number;
    /** Placement only, on the outer frame. */
    class?: string;
    children: Snippet;
  }

  let {
    columns = 3,
    masonry = false,
    minAspectRatio,
    class: className = '',
    children,
  }: Props = $props();

  // Fixed at mount: the pages that use it never switch modes on a live grid.
  // Masonry places every card itself, so `data-fill` applies only without it.
  function layout(node: HTMLElement, enabled: boolean) {
    return enabled ? masonryLayout(node) : cardFill(node);
  }
</script>

<div class="card-grid-frame {className}">
  <div
    class="card-grid-tracks"
    class:masonry
    class:aspect={minAspectRatio !== undefined}
    data-columns={columns}
    style:--card-grid-ratio={minAspectRatio}
    use:layout={masonry}
  >
    {@render children()}
  </div>
</div>

<style>
  /* The thresholds are literals because a container query cannot read var().
     The span rules sit beside them so a wide card never spans two columns on a
     one-column grid, which would add an implicit track and overflow. */
  .card-grid-frame {
    container: card-grid / inline-size;
    min-width: 0;
  }

  /* `--card-grid-track` is one column's width. It resolves on the element that
     reads it, against this frame's container, so a card can size from it. The
     gap follows `.card-pane`'s padding, so the space between cards is the
     space around them. */
  .card-grid-tracks {
    --card-grid-cols: 1;
    --card-grid-gap: var(--card-pane-pad, var(--space-4));
    --card-grid-track: calc(
      (100cqi - (var(--card-grid-cols) - 1) * var(--card-grid-gap)) / var(--card-grid-cols)
    );
    display: grid;
    grid-template-columns: repeat(var(--card-grid-cols), minmax(0, 1fr));
    gap: var(--card-grid-gap);
  }

  /* A min-height rather than aspect-ratio: under overflow: hidden an
     aspect-ratio is an exact height, and a long card would clip at it. */
  .card-grid-tracks.aspect > :global(*) {
    min-height: calc(var(--card-grid-track) / var(--card-grid-ratio));
  }

  .card-grid-tracks.masonry {
    align-items: start;
  }

  /* Set by the masonry action once it has placed the cards: rows become 1px
     units it spans, and the vertical gap is part of each card's span. */
  .card-grid-tracks:global([data-masonry='true']) {
    grid-auto-rows: 1px;
    row-gap: 0;
  }

  .card-grid-tracks > :global([data-width='full']) {
    grid-column: 1 / -1;
  }

  @container card-grid (min-width: 40rem) {
    .card-grid-tracks:not([data-columns='1']) {
      --card-grid-cols: 2;
    }
    .card-grid-tracks:not([data-columns='1']) > :global([data-width='wide']) {
      grid-column: span 2;
    }
  }

  @container card-grid (min-width: 64rem) {
    .card-grid-tracks:is([data-columns='3'], [data-columns='4'], [data-columns='5']) {
      --card-grid-cols: 3;
    }
  }

  @container card-grid (min-width: 86rem) {
    .card-grid-tracks:is([data-columns='4'], [data-columns='5']) {
      --card-grid-cols: 4;
    }
  }

  @container card-grid (min-width: 108rem) {
    .card-grid-tracks[data-columns='5'] {
      --card-grid-cols: 5;
    }
  }
</style>
