<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    /** The tile's caption, rendered as a `.micro-label` above the value. */
    label: string;
    /** Optional detail line under the value — a rate, a period, a comparison. */
    sub?: string;
    /** Centre the tile's text. Start-aligned otherwise. */
    align?: 'start' | 'center';
    /** Give the tile its own card surface. Off for a tile already inside one. */
    surface?: boolean;
    /** Placement only — a grid span, a column start. Never appearance. */
    class?: string;
    /**
     * Tint for the value, as a CSS colour — usually a token, e.g.
     * `var(--money-income)`. Every adopter tints its value and only its value,
     * but by its own axis (money by direction, admin by severity), so this is
     * a colour rather than a variant list. Falsy leaves the default.
     */
    valueColor?: string;
    /** Step the numeral size, e.g. `var(--text-base)` for a tight row. */
    valueSize?: string;
    /**
     * Outline a `surface` tile, as a CSS colour. Off by default: money's
     * summary strips are deliberately borderless (see `lib/styles/cards.css`),
     * while taxes outlines its cards and uses a stronger colour on the total
     * to rank it. Both are real choices, so neither is baked in.
     */
    borderColor?: string;
    /** The value itself. */
    children: Snippet;
  }

  let {
    label,
    sub,
    align = 'start',
    surface = false,
    class: className = '',
    valueColor = '',
    valueSize = '',
    borderColor = '',
    children,
  }: Props = $props();

  // Set as custom properties rather than styling `.stat-value` directly, so an
  // ancestor can set the same hooks for a whole row and a single tile can
  // still override it.
  const style = $derived(
    [
      valueColor ? `--stat-value-fg: ${valueColor}` : '',
      valueSize ? `--stat-value-size: ${valueSize}` : '',
      borderColor ? `--stat-border: ${borderColor}` : '',
    ]
      .filter(Boolean)
      .join('; '),
  );
</script>

<!--
  One labelled figure: the unit a KPI row or a summary strip is built out of.

  Four implementations before this existed — /admin's `.kpi`, money/taxes'
  `.summary-card`, and money's portfolio-overview and cash-flow tiles, the last
  two byte-identical to each other. Two tells that they were one thing: both
  money copies had retyped `.micro-label`'s declaration by hand as
  `.card-label`, and the four disagreed on ORDER, admin rendering label→value
  →sub against money's value→label. A reader met two different tiles for one
  idea depending on the page.

  So the component settles what was drift and keeps props only for what was a
  real choice. Order, label typography and the numeral treatment are fixed;
  `surface` and `align` are not, because a tile inside a bigger card must not
  draw a second one, and a four-across summary strip reads better centred than
  a dense twelve-across grid does.

  The value's colour is a custom property rather than a variant list: every
  adopter tints it, but by its own axis — money by direction, admin by
  severity — and a page's scoped CSS cannot reach into a child component.
  Same answer Badge gives with --badge-bg / --badge-fg.
-->
<div
  class="stat-tile {className}"
  class:stat-surface={surface}
  class:stat-bordered={!!borderColor}
  class:stat-center={align === 'center'}
  {style}
>
  <div class="micro-label">{label}</div>
  <div class="stat-value">{@render children()}</div>
  {#if sub}
    <div class="stat-sub">{sub}</div>
  {/if}
</div>

<style>
  .stat-tile {
    display: flex;
    flex-direction: column;
    /* design-lint-allow: hairline nudge below --space-1 — the label, value and
       sub are one block, and a full step between them reads as three separate
       things. The same 0.15rem the four tiles this replaces each used. */
    gap: 0.15rem;
    /* A long label in a grid track must be allowed to shrink, or the track
       floors at its content width and the row overflows on a phone. */
    min-width: 0;
  }

  .stat-surface {
    background: var(--surface-card);
    border-radius: var(--radius-card);
    /* One step all round. The copies sat at `--space-3 --space-2` (the centred
       money strips) and `--space-2 --space-3` (taxes' tighter row) — the same
       two values transposed, which is drift rather than a decision. */
    padding: var(--space-3);
  }

  /* Only when the caller asked for one — no transparent 1px placeholder, so a
     borderless strip keeps exactly the box it had. */
  /* design-lint-allow: --stat-border is the documented caller hook. */
  .stat-bordered {
    border: 1px solid var(--stat-border);
  }

  .stat-center {
    text-align: center;
  }

  .stat-value {
    /* design-lint-allow: --stat-value-fg / --stat-value-size are the documented
       hooks a caller sets on the element, so they are supplied from outside and
       are deliberately absent from the token roster. */
    font-size: var(--stat-value-size, var(--text-lg));
    font-weight: 600;
    /* Tabular figures, so a column of numbers lines up and a changing value
       does not shift the characters after it. */
    font-variant-numeric: tabular-nums;
    /* design-lint-allow: see above. */
    color: var(--stat-value-fg, var(--text-primary));
  }

  .stat-sub {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
</style>
