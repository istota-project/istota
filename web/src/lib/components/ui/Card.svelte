<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    title?: string;
    /** Replaces `title` when the heading needs markup. */
    heading?: Snippet;
    /** The heading element: an `h2` unless the card sits under another section heading. */
    level?: 2 | 3;
    /** The line under the title, in the `.description` role. */
    description?: string;
    /** Before the title (a connection card's logo). */
    icon?: Snippet;
    /** Beside the title (a status pill or badge). */
    status?: Snippet;
    /** The header's far end: actions that belong to this card. */
    actions?: Snippet;
    /** A flush-right `.form-actions` row closing the card. */
    footer?: Snippet;
    /**
     * `default` the bordered card, `flat` the borderless tile, `inset` a card on
     * `--surface-base`, `raised` an inner panel on `--surface-raised`, `outline`
     * the border with no fill.
     */
    variant?: 'default' | 'flat' | 'inset' | 'raised' | 'outline';
    /** `md` steps down at 768px and 640px; `sm` is one step all round. */
    padding?: 'sm' | 'md';
    /** The border lifts on hover. Implied by `href`. */
    interactive?: boolean;
    /** Renders the card as a link. */
    href?: string;
    /** With `interactive`, renders the card as a button. */
    onclick?: (e: MouseEvent) => void;
    /** Accessible name for a button card whose content does not name it. */
    ariaLabel?: string;
    as?: 'section' | 'article' | 'div';
    /** Placement only (a grid span, a margin), never appearance. */
    class?: string;
    children: Snippet;
    [attr: `data-${string}` | `aria-${string}`]: unknown;
  }

  let {
    title,
    heading,
    level = 2,
    description,
    icon,
    status,
    actions,
    footer,
    variant = 'default',
    padding = 'md',
    interactive = false,
    href,
    onclick,
    ariaLabel,
    as = 'section',
    class: className = '',
    children,
    ...rest
  }: Props = $props();

  const tag = $derived(href ? 'a' : interactive && onclick ? 'button' : as);
  const hasHead = $derived(!!(title || heading || icon || status || actions));
  const hasTitle = $derived(!!(title || heading));
</script>

<!--
  The card look, owned by the component rather than by an ancestor class. It
  was `.settings .card` in settings.css and `.health-frame :global(.card)` in
  the health layout, so every page that wanted the look outside those two
  wrapped itself in `class="settings"` to borrow it, and 22 sections wrote the
  markup by hand because `SettingsCard` took no class or attributes.
-->
<svelte:element
  this={tag}
  class="ui-card ui-card-{variant} ui-card-pad-{padding} {className}"
  class:ui-card-interactive={interactive || !!href}
  href={tag === 'a' ? href : undefined}
  type={tag === 'button' ? 'button' : undefined}
  onclick={tag === 'button' ? onclick : undefined}
  aria-label={ariaLabel}
  {...rest}
>
  {#if hasHead}
    <header class="ui-card-head">
      {#if icon || hasTitle || status}
        <div class="hstack ui-card-title">
          {#if icon}{@render icon()}{/if}
          {#if heading}
            <svelte:element this={`h${level}`} class="title-section"
              >{@render heading()}</svelte:element
            >
          {:else if title}
            <svelte:element this={`h${level}`} class="title-section">{title}</svelte:element>
          {/if}
          {#if status}{@render status()}{/if}
        </div>
      {/if}
      {#if actions}
        <div class="hstack">{@render actions()}</div>
      {/if}
    </header>
  {/if}
  {#if description}<p class="description">{description}</p>{/if}
  {@render children()}
  {#if footer}
    <div class="form-actions">{@render footer()}</div>
  {/if}
</svelte:element>

<style>
  .ui-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    background: var(--surface-card);
    box-sizing: border-box;
    /* A grid or flex item that may shrink below its content, as a card in a
       wall of them must. */
    min-width: 0;
  }

  .ui-card-flat {
    border: 0;
  }

  .ui-card-inset {
    background: var(--surface-base);
  }

  .ui-card-raised {
    background: var(--surface-raised);
    border-color: var(--border-subtle);
  }

  .ui-card-outline {
    background: none;
  }

  .ui-card-pad-md {
    padding: var(--space-4);
  }

  .ui-card-pad-sm {
    padding: var(--space-3);
  }

  @media (max-width: 768px) {
    .ui-card-pad-md {
      padding: var(--space-3);
    }
  }

  @media (max-width: 640px) {
    .ui-card-pad-md {
      padding: var(--space-2);
    }
  }

  /* A link or button card reads as its content: no underline, no UA button face. */
  a.ui-card,
  button.ui-card {
    color: inherit;
    text-decoration: none;
  }

  button.ui-card {
    font: inherit;
    text-align: start;
    cursor: pointer;
  }

  .ui-card-interactive {
    transition: border-color var(--transition-fast);
  }

  .ui-card-interactive:hover {
    border-color: var(--border-hover);
  }

  .ui-card-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  /* The card's gap spaces its children, so a browser paragraph margin would double it. */
  .ui-card :global(:where(p, ul, ol)) {
    margin-block: 0;
  }

  /* A button or link placed straight in the card's column keeps its own width
     rather than stretching across the card. */
  .ui-card > :global(:where(button, a)) {
    align-self: flex-start;
  }

  /* A sub-heading inside a card. At element weight, so a role class on the
     heading (`.micro-label`, `.title-card`) or a page rule wins outright. A
     level-3 card title is not a sub-heading and keeps the title colour. */
  :global(:where(.ui-card) h3:where(:not(.title-section))) {
    font-size: var(--text-sm);
    font-weight: var(--weight-semibold);
    color: var(--text-muted);
  }
</style>
