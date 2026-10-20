<script lang="ts">
  import type { Component, Snippet } from 'svelte';
  import Icon, { type IconSize } from './Icon.svelte';

  type Size = 'sm' | 'md' | 'round';

  interface Props {
    /**
     * Required, and becomes the aria-label. An icon-only button has no text to
     * name it, and 146 bare <button>s in this tree relied on the author
     * remembering to add one — several did not.
     */
    label: string;
    size?: Size;
    class?: string;
    /** Hover turns the glyph red rather than raising it. For a delete action. */
    danger?: boolean;
    /** Pressed/open state, for a menu trigger. */
    active?: boolean;
    pressed?: boolean;
    type?: 'button' | 'submit';
    title?: string;
    disabled?: boolean;
    /**
     * Renders a real `<a>` with the same look, for an action that is a link
     * (a download, an external page) — a button calling `window.open` loses
     * the native `download` attribute and middle-click. A disabled one renders
     * as a disabled button, since a link has no disabled state.
     */
    href?: string;
    download?: string;
    target?: string;
    rel?: string;
    onclick?: (e: MouseEvent) => void;
    /**
     * The glyph, drawn through `Icon` at the button's own step (`sm` and
     * `md` alike); `iconSize` for a toolbar or dialog glyph a step up.
     * Children instead, for anything that is not one lucide glyph.
     */
    icon?: Component;
    iconSize?: IconSize;
    children?: Snippet;
  }

  let {
    label,
    size = 'md',
    class: className = '',
    danger = false,
    active = false,
    pressed,
    type = 'button',
    title,
    disabled,
    href,
    download,
    target,
    rel,
    onclick,
    icon,
    iconSize,
    children,
  }: Props = $props();

  const glyphSize = $derived(iconSize ?? (size === 'sm' ? 'sm' : 'md'));
</script>

{#snippet glyph()}
  {#if icon}<Icon {icon} size={glyphSize} />{:else if children}{@render children()}{/if}
{/snippet}

{#if href && !disabled}
  <a
    class="icon-btn icon-btn-{size} {className}"
    class:danger
    class:active
    {href}
    {download}
    {target}
    {rel}
    {title}
    {onclick}
    aria-label={label}
  >
    {@render glyph()}
  </a>
{:else}
  <button
    class="icon-btn icon-btn-{size} {className}"
    class:danger
    class:active
    {type}
    {title}
    {disabled}
    {onclick}
    aria-label={label}
    aria-pressed={pressed}
  >
    {@render glyph()}
  </button>
{/if}

<style>
  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--text-dim);
    cursor: pointer;
    /* A <button> does not inherit font. Without this the em sizes below resolve
       against the UA's ~13px, so the control stops tracking the text-scale
       preference while the type around it grows. Two of the four hand-rolled
       icon buttons this replaces were missing it. */
    font: inherit;
    /* Opts out of double-tap-to-zoom, which is what makes iOS hold a tap open
       and then deliver it as a delayed synthesized click. */
    touch-action: manipulation;
    transition:
      color var(--transition-fast),
      background var(--transition-fast);
  }

  a.icon-btn {
    text-decoration: none;
  }

  .icon-btn:hover:not(:disabled),
  .icon-btn.active {
    background: var(--hover-bg);
    color: var(--hover-fg);
  }

  .icon-btn.danger:hover:not(:disabled) {
    background: transparent;
    color: var(--status-danger-fg);
  }

  .icon-btn:disabled {
    opacity: 0.3;
    cursor: not-allowed;
  }

  /* Both heights are inert at their own tier — the glyph and its padding
     already come to about this much — and are here so an icon action lines up
     with the text input beside it inside a `.control-row`, which raises both
     tiers. The corner is already the field shape, so it stays off the
     --control-radius channel. */
  .icon-btn-sm {
    min-height: var(--control-height-sm);
    padding: 0.15rem var(--space-2);
    border-radius: var(--radius-sm);
  }

  .icon-btn-md {
    min-height: var(--control-height-md);
    padding: var(--space-1);
    border-radius: var(--radius-sm);
  }

  /* A circular target sized in em so it tracks the text scale — the composer's
     mic and send, where the control sits in a row with the text it belongs to. */
  .icon-btn-round {
    flex-shrink: 0;
    width: 2.35em;
    height: 2.35em;
    border-radius: var(--radius-round);
  }

  .icon-btn-round :global(.ui-icon),
  .icon-btn-round :global(svg) {
    width: 1.25em;
    height: 1.25em;
  }
</style>
