<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    /** The narrowest a column gets before the grid drops one. */
    min?: string;
    /** A step of the spacing ramp between cells. */
    gap?: 2 | 3;
    children: Snippet;
    [attr: `data-${string}` | `aria-${string}`]: unknown;
  }

  let { min = '10rem', gap = 3, children, ...rest }: Props = $props();

  const GAPS = { 2: 'var(--space-2)', 3: 'var(--space-3)' } as const;
</script>

<!--
  Fields in as many columns as fit. The minimum is in rem so it tracks the
  text-scale preference, and `min()` keeps a column from overflowing a
  container narrower than it, which one health form's bare minmax() did. A
  field that should take a row of its own carries `class="grid-full"`.
-->
<div class="ui-form-grid" style:--form-grid-min={min} style:gap={GAPS[gap]} {...rest}>
  {@render children()}
</div>

<style>
  .ui-form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(var(--form-grid-min), 100%), 1fr));
    min-width: 0;
  }

  .ui-form-grid > :global(.grid-full) {
    grid-column: 1 / -1;
  }
</style>
