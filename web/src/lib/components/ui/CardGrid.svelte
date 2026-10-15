<script lang="ts" module>
  export type CardGridColumns = 1 | 2 | 3 | 4 | 5;
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';
  import { masonry as masonryLayout } from '$lib/masonry';

  interface Props {
    /** The most columns the grid grows to; it steps down to one as its container narrows. */
    columns?: CardGridColumns;
    masonry?: boolean;
    /** Placement only, on the outer frame. */
    class?: string;
    children: Snippet;
  }

  let { columns = 3, masonry = false, class: className = '', children }: Props = $props();

  // Fixed at mount: the pages that use it never switch modes on a live grid.
  function layout(node: HTMLElement, enabled: boolean) {
    return enabled ? masonryLayout(node) : {};
  }
</script>

<div class="card-grid-frame {className}">
  <div class="card-grid-tracks" class:masonry data-columns={columns} use:layout={masonry}>
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

  .card-grid-tracks {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-4);
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
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .card-grid-tracks:not([data-columns='1']) > :global([data-width='wide']) {
      grid-column: span 2;
    }
  }

  @container card-grid (min-width: 64rem) {
    .card-grid-tracks:is([data-columns='3'], [data-columns='4'], [data-columns='5']) {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
  }

  @container card-grid (min-width: 86rem) {
    .card-grid-tracks:is([data-columns='4'], [data-columns='5']) {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
  }

  @container card-grid (min-width: 108rem) {
    .card-grid-tracks[data-columns='5'] {
      grid-template-columns: repeat(5, minmax(0, 1fr));
    }
  }
</style>
