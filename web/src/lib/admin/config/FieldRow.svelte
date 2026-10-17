<script lang="ts">
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();
</script>

<!--
  One config field's row: the field (label and control), then whatever acts on
  it (Clear, Reset, Undo) and its status at the far end. From 641px, where an
  inline Field puts its label beside the control, the row never wraps: the
  control gives up width first, so a button never drops under the label. Below
  that the Field stacks its label over the control, and the row stacks with it.
-->
<div class="config-row">{@render children()}</div>

<style>
  .config-row {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  @media (min-width: 641px) {
    .config-row {
      flex-direction: row;
      align-items: flex-start;
      column-gap: var(--space-2);
    }

    .config-row > :global(.field) {
      flex: 0 1 calc(var(--field-label-width) + var(--space-3) + 24rem);
      min-width: 0;
    }

    .config-row > :global(.field.field-wide) {
      flex-basis: calc(var(--field-label-width) + var(--space-3) + 36rem);
    }
  }
</style>
