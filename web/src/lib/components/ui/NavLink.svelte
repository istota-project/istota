<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    href: string;
    active?: boolean;
    children: Snippet;
  }

  let { href, active = false, children }: Props = $props();
</script>

<a {href} class="nav-link" class:active>{@render children()}</a>

<style>
  /* On the shared control channel, like `Chip`. Inert in the app bar — the sm
     tier is exactly what this already computed to (0.75rem text at 1.2
     leading plus 0.15rem either side) and the root radius is the pill it
     already had — so it bites only where a container raises the tier. That is
     a body section header, where these links share a row with a filter, and
     where a section holding only a nav has to come out the same height as one
     that also has a Select: otherwise the bar changes height as you move
     between tabs of the same module. */
  .nav-link {
    display: inline-flex;
    align-items: center;
    min-height: var(--control-height-sm);
    font-size: var(--text-sm);
    line-height: 1.2;
    color: var(--text-muted);
    text-decoration: none;
    padding: 0.15rem var(--chip-padding-x);
    border-radius: var(--control-radius);
    transition: all var(--transition-fast);
  }

  .nav-link:hover {
    color: var(--text-primary);
  }

  .nav-link.active {
    background: var(--surface-raised);
    color: var(--text-primary);
  }
</style>
