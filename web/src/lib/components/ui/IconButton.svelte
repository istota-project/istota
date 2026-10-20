<script lang="ts">
  import type { Component, Snippet } from 'svelte';
  import Icon, { type IconSize } from './Icon.svelte';
  import Spinner from './Spinner.svelte';
  import { linkOrButton } from './internal/linkOrButton';

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
    /**
     * `muted` rests at `--text-muted` and hovers to `--accent-hover` on
     * `--surface-badge`, only where the pointer can hover: the chat
     * composer's tools, which sit over the transcript rather than on a card.
     */
    tone?: 'default' | 'muted';
    /** Pressed/open state, for a menu trigger. */
    active?: boolean;
    pressed?: boolean;
    /** In flight: disabled, `aria-busy`, and a spinner in place of the glyph. */
    loading?: boolean;
    /** Adds the shared `.touch-target` overlay, for a glyph below the touch floor. */
    touchTarget?: boolean;
    type?: 'button' | 'submit';
    title?: string;
    disabled?: boolean;
    /**
     * Renders a real `<a>` with the same look, for an action that is a link
     * (a download, an external page) — a button calling `window.open` loses
     * the native `download` attribute and middle-click. A disabled or loading
     * one renders as a disabled button, since a link has no disabled state.
     */
    href?: string;
    download?: string;
    target?: string;
    rel?: string;
    onclick?: (e: MouseEvent) => void;
    /** The rendered element, for a caller that measures or focuses it. */
    ref?: HTMLElement;
    /**
     * The glyph, drawn through `Icon` at the button's own step (`sm` and
     * `md` alike); `iconSize` for a toolbar or dialog glyph a step up.
     * Children instead, for anything that is not one lucide glyph.
     */
    icon?: Component;
    iconSize?: IconSize;
    children?: Snippet;
    /** Other attributes and handlers go to the element (a menu trigger's props). */
    [attr: string]: unknown;
  }

  let {
    label,
    size = 'md',
    class: className = '',
    danger = false,
    tone = 'default',
    active = false,
    pressed,
    loading = false,
    touchTarget = false,
    type = 'button',
    title,
    disabled,
    href,
    download,
    target,
    rel,
    onclick,
    ref = $bindable(),
    icon,
    iconSize,
    children,
    ...rest
  }: Props = $props();

  const glyphSize = $derived(iconSize ?? (size === 'sm' ? 'sm' : 'md'));
  const el = $derived(linkOrButton({ href, download, target, rel, type, disabled, loading }));
</script>

<svelte:element
  this={el.tag}
  bind:this={ref}
  {...rest}
  class="icon-btn icon-btn-{size} {className}"
  class:danger
  class:muted={tone === 'muted'}
  class:active
  class:touch-target={touchTarget}
  {...el.attrs}
  {title}
  {onclick}
  aria-label={label}
  aria-pressed={el.tag === 'button' ? pressed : undefined}
>
  {#if loading}<Spinner size="sm" />{:else if icon}<Icon
      {icon}
      size={glyphSize}
    />{:else if children}{@render children()}{/if}
</svelte:element>

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

  .icon-btn.muted {
    color: var(--text-muted);
  }
  /* iOS synthesizes :hover on a tap and leaves it applied, so a muted button
     keeps no hover fill on a touch screen; the fill is for a pointer. */
  .icon-btn.muted:hover:not(:disabled) {
    background: transparent;
    color: var(--text-muted);
  }
  @media (hover: hover) {
    .icon-btn.muted:hover:not(:disabled) {
      background: var(--surface-badge);
      color: var(--accent-hover);
    }
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
