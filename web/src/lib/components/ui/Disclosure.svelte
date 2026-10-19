<!--
  Show and hide a block behind a summary line. A native <details>, so it works
  without script and opens on find-in-page; the one marker is a lucide
  ChevronRight that turns down when open.
-->
<script lang="ts">
  import { untrack, type Snippet } from 'svelte';
  import { ChevronRight } from '@lucide/svelte';
  import { loadSetting, saveSetting } from '$lib/stores/persisted';

  interface Props {
    summary: string | Snippet;
    /** Bindable. Seeded from `storageKey` when one is given. */
    open?: boolean;
    /** `inline` (the default): a quiet line in the flow. `card`: a bordered
     *  panel. `section`: a report section header, with `trailing` at its end. */
    variant?: 'inline' | 'card' | 'section';
    /** A `card` inside another card: the inner-panel look, on
     *  `--surface-raised` at the small padding. */
    nested?: boolean;
    /** After the summary, at the row's far end (a section's total). */
    trailing?: Snippet;
    /** Remembers the open state in browser storage under this key. */
    storageKey?: string;
    /** Placement only. */
    class?: string;
    children: Snippet;
  }

  let {
    summary,
    open = $bindable(false),
    variant = 'inline',
    nested = false,
    trailing,
    storageKey,
    class: className = '',
    children,
  }: Props = $props();

  untrack(() => {
    if (!storageKey) return;
    const saved = loadSetting<unknown>(storageKey, open);
    if (typeof saved === 'boolean') open = saved;
  });

  // Read off the element: the toggle event can reach this before bind:open has
  // written the new value back.
  function ontoggle(e: Event & { currentTarget: HTMLDetailsElement }) {
    if (storageKey) saveSetting(storageKey, e.currentTarget.open);
  }
</script>

<details
  class="ui-disclosure variant-{variant} {className}"
  class:nested={nested && variant === 'card'}
  bind:open
  {ontoggle}
>
  <summary class="ui-disclosure-summary">
    <span class="ui-disclosure-marker"><ChevronRight aria-hidden="true" /></span>
    <span class="ui-disclosure-label"
      >{#if typeof summary === 'string'}{summary}{:else}{@render summary()}{/if}</span
    >
    {#if trailing}<span class="ui-disclosure-trailing">{@render trailing()}</span>{/if}
  </summary>
  <div class="ui-disclosure-body">
    {@render children()}
  </div>
</details>

<style>
  .ui-disclosure-summary {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    list-style: none;
    cursor: pointer;
    user-select: none;
    font-size: var(--text-sm);
    color: var(--text-muted);
    transition: color var(--transition-fast);
  }
  .ui-disclosure-summary::-webkit-details-marker {
    display: none;
  }
  .ui-disclosure-summary:hover {
    color: var(--text-secondary);
  }

  .ui-disclosure-marker {
    display: inline-flex;
    flex: none;
    width: var(--icon-xs);
    height: var(--icon-xs);
    transition: transform var(--transition-fast);
  }
  .ui-disclosure-marker :global(svg) {
    width: 100%;
    height: 100%;
  }
  .ui-disclosure[open] > .ui-disclosure-summary .ui-disclosure-marker {
    transform: rotate(90deg);
  }

  .ui-disclosure-label {
    min-width: 0;
  }

  .ui-disclosure-trailing {
    margin-left: auto;
  }

  /* A quiet line takes only its own width, so the empty end of the row is not
     a click target. */
  .variant-inline > .ui-disclosure-summary {
    width: fit-content;
    max-width: 100%;
  }
  .variant-inline > .ui-disclosure-body {
    margin-top: var(--space-2);
  }

  /* The look and padding of a default `Card`, so a collapsed panel is the same
     box as the cards around it. The padding sits on the summary and the body
     rather than the <details>, or the click target would stop short of the
     panel's edges. */
  .variant-card {
    --disclosure-pad: var(--space-4);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    background: var(--disclosure-bg, var(--surface-card));
  }
  .variant-card > .ui-disclosure-summary {
    padding: var(--disclosure-pad);
  }
  .variant-card > .ui-disclosure-body {
    padding: 0 var(--disclosure-pad) var(--disclosure-pad);
  }
  @media (max-width: 768px) {
    .variant-card {
      --disclosure-pad: var(--space-3);
    }
  }
  @media (max-width: 640px) {
    .variant-card {
      --disclosure-pad: var(--space-2);
    }
  }
  /* Inside a card: the inner-panel look (`Card variant="raised"` padding sm). */
  .variant-card.nested {
    --disclosure-pad: var(--space-3);
    --disclosure-bg: var(--surface-raised);
  }

  .variant-section > .ui-disclosure-summary {
    gap: var(--space-2);
    padding: var(--space-3) var(--space-3) var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-primary);
  }
  .variant-section:not(:first-child) {
    margin-top: var(--space-2);
  }

  @media (prefers-reduced-motion: reduce) {
    .ui-disclosure-marker {
      transition: none;
    }
  }
</style>
