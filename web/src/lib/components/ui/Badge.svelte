<svelte:options runes={true} />

<script lang="ts" module>
  export type BadgeVariant = 'neutral' | 'danger' | 'warn' | 'success' | 'info' | 'partial';
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';

  type Variant = BadgeVariant;

  interface Props {
    variant?: Variant;
    /** `sm` is the admin dashboard's compact chip, for a badge sitting
     *  beside dense row text. */
    size?: 'md' | 'sm';
    /**
     * `solid` fills with the variant's `-bg`. `subtle` is the quiet pill on
     * `--surface-raised` with only the text in the variant's colour: a
     * service's state, a feed's type, a file's kind.
     */
    tone?: 'solid' | 'subtle';
    /** A status label is caps; a name or a kind (a mime type, a count) is not. */
    uppercase?: boolean;
    /** A glyph before the text. */
    icon?: Snippet;
    /** Placement only. */
    class?: string;
    /** The tooltip, on the pill itself. Never wrap a badge in a `<span title>`:
     *  in a flex row the wrapper's line box puts the pill a few pixels above
     *  an unwrapped neighbour. */
    title?: string;
    children: Snippet;
  }

  let {
    variant = 'neutral',
    size = 'md',
    tone = 'solid',
    uppercase = true,
    icon,
    class: className = '',
    title,
    children,
  }: Props = $props();
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
<span
  class="badge badge-{variant} {className}"
  class:badge-sm={size === 'sm'}
  class:badge-subtle={tone === 'subtle'}
  class:badge-lower={!uppercase}
  {title}>{@render icon?.()}{@render children()}</span
>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: var(--tracking-caps);
    padding: 0.1rem var(--space-2);
    border-radius: var(--radius-pill);
    font-weight: var(--weight-medium);
    /* A status label that wraps mid-word stops reading as one token, and its
       column then shifts row by row. */
    white-space: nowrap;
    /* design-lint-allow-begin: --badge-bg / --badge-fg are the documented
       hook a categorical caller sets on the element; they are supplied from
       outside, so they are deliberately absent from the token roster. */
    background: var(--badge-bg, var(--surface-badge));
    color: var(--badge-fg, var(--text-muted));
  }

  .badge-sm {
    /* design-lint-allow: the admin dashboard's compact chip, below --space-1. */
    padding: 0.05rem var(--space-2);
    font-size: var(--text-2xs);
    font-weight: var(--weight-semibold);
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

  /* After the variants, so it outranks their fills: the variant colours only
     the text. The quiet pill .settings .status-pill, feeds' type column and
     the health tags each wrote by hand. */
  .badge-subtle {
    /* design-lint-allow: the quiet pill's hairline padding, below --space-1. */
    padding: 0.05rem var(--space-2);
    background: var(--surface-raised);
    color: var(--text-muted);
    font-weight: var(--weight-regular);
  }
  .badge-subtle.badge-danger {
    color: var(--status-danger-fg);
  }
  .badge-subtle.badge-warn {
    color: var(--status-warn-fg);
  }
  .badge-subtle.badge-success {
    color: var(--status-success-fg);
  }
  .badge-subtle.badge-info {
    color: var(--status-info-fg);
  }
  .badge-subtle.badge-partial {
    color: var(--status-partial-fg);
  }

  .badge-lower {
    text-transform: none;
    letter-spacing: normal;
  }
</style>
