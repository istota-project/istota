<script lang="ts">
  import type { Snippet } from 'svelte';

  type Variant = 'neutral' | 'danger' | 'warn' | 'success' | 'info' | 'partial';

  interface Props {
    variant?: Variant;
    children: Snippet;
  }

  let { variant = 'neutral', children }: Props = $props();
</script>

<!--
  A small uppercase status pill. Seven byte-identical copies of these rules
  were scattered across the health pages, each pairing a --status-*-fg token
  with a hand-written hsla() fill that was really the matching -bg token
  written out by hand — and so frozen to its dark value.

  A categorical badge, where the hue names a *kind* rather than a severity
  (an encounter type, a vaccine series that is part-done rather than late),
  sets --badge-bg and --badge-fg on the element instead of picking a variant.
  Custom properties rather than a class, because a page's scoped CSS cannot
  reach inside a child component to restyle it.
-->
<span class="badge badge-{variant}">{@render children()}</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.1rem 0.5rem;
    border-radius: var(--radius-pill);
    font-weight: 500;
    /* A status label that wraps mid-word stops reading as one token, and its
       column then shifts row by row. */
    white-space: nowrap;
    /* design-lint-allow-begin: --badge-bg / --badge-fg are the documented
       hook a categorical caller sets on the element; they are supplied from
       outside, so they are deliberately absent from the token roster. */
    background: var(--badge-bg, var(--surface-badge));
    color: var(--badge-fg, var(--text-muted));
  }

  .badge-danger {
    background: var(--badge-bg, var(--status-danger-bg));
    color: var(--badge-fg, var(--status-danger-fg));
  }

  .badge-warn {
    background: var(--badge-bg, var(--status-warn-bg));
    color: var(--badge-fg, var(--status-warn-fg));
  }

  .badge-success {
    background: var(--badge-bg, var(--status-success-bg));
    color: var(--badge-fg, var(--status-success-fg));
  }

  .badge-info {
    background: var(--badge-bg, var(--status-info-bg));
    color: var(--badge-fg, var(--status-info-fg));
  }

  /* Off the severity ramp on purpose: part-done, not late. */
  .badge-partial {
    background: var(--badge-bg, var(--status-partial-bg));
    color: var(--badge-fg, var(--status-partial-fg));
    /* design-lint-allow-end */
  }
</style>
