<svelte:options runes={true} />

<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    /** Renders the row as a link, which keeps middle-click and the URL preview. */
    href?: string;
    /** Without `href`, renders the row as a button. With neither, the row is inert. */
    onclick?: (e: MouseEvent) => void;
    /** The current item: the row fill and `--text-primary`; a link also gets `aria-current="page"`. */
    active?: boolean;
    /** `base` (`--text-base`: chat, section lists) or `sm` (`--text-sm`: feeds, places, accounts, the archive). */
    density?: 'base' | 'sm';
    /** A glyph before the label. */
    icon?: Snippet;
    /** Straight after the label: a state glyph, an unread mark, a reach icon. */
    mark?: Snippet;
    /** At the far end of the label line, inside the hit target: counts. */
    badge?: Snippet;
    /** Lines under the label (the briefings archive's audience and preview). */
    meta?: Snippet;
    /** Beside the hit target, outside it: a `KebabMenu`. */
    trailing?: Snippet;
    /** A colour washed over the row (chat's room colour). */
    tint?: string;
    /** Placement only, on the row. */
    class?: string;
    title?: string;
    target?: string;
    rel?: string;
    'aria-current'?: 'page' | 'location' | 'true' | boolean;
    children: Snippet;
    [attr: `aria-${string}` | `data-${string}`]: unknown;
  }

  let {
    href,
    onclick,
    active = false,
    density = 'base',
    icon,
    mark,
    badge,
    meta,
    trailing,
    tint,
    class: className = '',
    'aria-current': ariaCurrent,
    children,
    ...rest
  }: Props = $props();

  const tag = $derived(href ? 'a' : onclick ? 'button' : 'div');
  const current = $derived(ariaCurrent ?? (href && active ? 'page' : undefined));
</script>

<!--
  One sidebar row: `.sidebar .list-row` and `.view-btn` folded into one
  element, with the five row buttons each sidebar used to write for itself.
  The row draws the hover and active fill, so a trailing kebab sits on it too;
  the hit target inside it carries the type and the padding.
-->
<div
  class="ui-list-row density-{density} {className}"
  class:active
  class:interactive={tag !== 'div'}
  class:tinted={!!tint}
  class:has-trailing={!!trailing}
  style:--row-tint={tint}
>
  <svelte:element
    this={tag}
    class="ui-list-row-hit"
    href={tag === 'a' ? href : undefined}
    type={tag === 'button' ? 'button' : undefined}
    onclick={tag === 'div' ? undefined : onclick}
    aria-current={tag === 'div' ? undefined : current}
    {...rest}
  >
    {#if icon}<span class="ui-list-row-icon">{@render icon()}</span>{/if}
    <span class="ui-list-row-body">
      <span class="ui-list-row-line">
        <span class="ui-list-row-label">{@render children()}</span>
        {#if mark}<span class="ui-list-row-mark">{@render mark()}</span>{/if}
        {#if badge}<span class="ui-list-row-badge">{@render badge()}</span>{/if}
      </span>
      {#if meta}<span class="ui-list-row-meta">{@render meta()}</span>{/if}
    </span>
  </svelte:element>
  {#if trailing}{@render trailing()}{/if}
</div>

<style>
  .ui-list-row {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .ui-list-row.has-trailing {
    padding-right: var(--space-1);
  }

  .ui-list-row.interactive:hover {
    background: var(--hover-bg);
  }

  .ui-list-row.active {
    background: var(--selected-bg);
  }

  /* The tint mixes into the hover and active fills rather than being painted
     over by them, so a coloured row keeps its colour under the pointer. */
  .ui-list-row.tinted {
    background: color-mix(in srgb, var(--row-tint) 14%, transparent);
  }

  .ui-list-row.tinted.interactive:hover,
  .ui-list-row.tinted.active {
    background: color-mix(in srgb, var(--row-tint) 26%, var(--hover-bg));
  }

  .ui-list-row-hit {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex: 1;
    min-width: 0;
    background: none;
    border: none;
    color: var(--text-muted);
    font: inherit;
    text-align: left;
    text-decoration: none;
    cursor: pointer;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
    /* Full-bleed in a clipped list: the ring is drawn inside the row. */
    outline-offset: calc(-1 * var(--focus-ring-width));
  }

  div.ui-list-row-hit {
    cursor: default;
  }

  .density-base .ui-list-row-hit {
    font-size: var(--text-base);
  }

  .density-sm .ui-list-row-hit {
    font-size: var(--text-sm);
    padding: var(--space-1) var(--space-3);
  }

  .ui-list-row.active .ui-list-row-hit {
    color: var(--text-primary);
  }

  .ui-list-row-icon {
    display: inline-flex;
    flex-shrink: 0;
  }

  .ui-list-row-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    flex: 1;
    min-width: 0;
  }

  .ui-list-row-line {
    display: flex;
    align-items: center;
    min-width: 0;
  }

  .ui-list-row-label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .ui-list-row-mark {
    display: inline-flex;
    flex-shrink: 0;
    margin-left: var(--space-1);
  }

  .ui-list-row-badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
    margin-left: auto;
    padding-left: var(--space-2);
  }

  .ui-list-row-meta {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  @media (pointer: coarse) {
    .ui-list-row {
      gap: var(--space-2);
    }
  }
</style>
