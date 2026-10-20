<script lang="ts">
  import type { Component, Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  import { linkOrButton } from './internal/linkOrButton';

  type Variant = 'primary' | 'secondary' | 'pill' | 'danger';
  type Size = 'sm' | 'md';

  interface Props {
    variant?: Variant;
    size?: Size;
    type?: 'button' | 'submit' | 'reset';
    /**
     * Renders a real `<a>` rather than a button. An action that navigates
     * should use this: it keeps middle-click, open-in-new-tab and the
     * status-bar URL preview, none of which an onclick + goto() has.
     * `KebabItem` takes the same option for the same reason.
     */
    href?: string;
    /** Passed through on an `href` button — for a download or an external tab. */
    download?: string;
    target?: string;
    rel?: string;
    /**
     * On an `href` button, make the client do a full navigation. For a path
     * the server answers itself (a redirect), which the prerendered frontend
     * has no route for and would otherwise try to route client-side.
     */
    reload?: boolean;
    onclick?: (e: MouseEvent) => void;
    title?: string;
    disabled?: boolean;
    /**
     * In-flight state. Disables the button and swaps its label for
     * `loadingLabel`, which is the shape twenty-seven `{saving ? 'Saving…' :
     * 'Save'}` ternaries were open-coding — each with its own idea of whether
     * to also disable, so several stayed clickable mid-save.
     */
    loading?: boolean;
    loadingLabel?: string;
    ariaLabel?: string;
    /** A glyph before the label, drawn through `Icon` at the label's step. */
    icon?: Component;
    /** Placement only: a page positions the button, never restyles it. */
    class?: string;
    children: Snippet;
  }

  let {
    variant = 'pill',
    size = 'md',
    type = 'button',
    href,
    download,
    target,
    rel,
    reload = false,
    onclick,
    title,
    disabled,
    loading = false,
    loadingLabel = 'Saving…',
    ariaLabel,
    icon,
    class: className = '',
    children,
  }: Props = $props();

  const el = $derived(
    linkOrButton({ href, download, target, rel, reload, type, disabled, loading }),
  );
</script>

<svelte:element
  this={el.tag}
  class="btn btn-{variant} btn-{size} {className}"
  {...el.attrs}
  {title}
  {onclick}
  aria-label={ariaLabel}
>
  {#if loading}{loadingLabel}{:else}{#if icon}<Icon
        {icon}
        size={size === 'sm' ? 'xs' : 'sm'}
      />{/if}{@render children()}{/if}
</svelte:element>

<style>
  .btn {
    display: inline-flex;
    /* An <a> carries link chrome the button variants do not want. */
    text-decoration: none;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    border: none;
    font: inherit;
    font-size: var(--text-sm);
    line-height: var(--leading-tight);
    border-radius: var(--control-radius);
    cursor: pointer;
    transition: all var(--transition-fast);
    user-select: none;
  }

  .btn:disabled {
    opacity: var(--disabled-opacity);
    cursor: not-allowed;
  }

  .btn-sm {
    min-height: var(--control-height-sm);
    padding: 0.15rem var(--space-2);
    font-size: var(--text-xs);
  }
  .btn-md {
    min-height: var(--control-height-md);
    padding: var(--space-1) var(--space-2);
  }

  .btn-primary {
    background: var(--accent);
    color: var(--surface-base);
  }
  .btn-primary:hover:not(:disabled) {
    background: var(--accent-hover);
  }

  /* Same filled shape as primary, quieter colour — for the reversing half of
	   a pair (Disconnect next to Connect) and every lesser action beside a
	   primary one. There is no transparent variant: a label with no chrome at
	   rest reads as a link set slightly off the text column. */
  .btn-secondary {
    background: var(--surface-raised);
    box-shadow: inset 0 0 0 1px var(--border-default);
    color: var(--text-muted);
  }
  .btn-secondary:hover:not(:disabled) {
    background: var(--border-default);
    color: var(--text-primary);
  }

  /* The default. Filled and outlined at rest so it stays visible on a card,
     whose background its old --surface-card fill matched exactly. */
  .btn-pill {
    background: var(--surface-raised);
    box-shadow: inset 0 0 0 1px var(--border-default);
    color: var(--text-primary);
  }
  .btn-pill:hover:not(:disabled) {
    background: var(--border-default);
  }

  /* Red outline/text for a destructive confirm action (the confirm button in
	   ConfirmDialog). Matches the hand-rolled .btn.danger convention it replaces
	   (#c66 dark / #c0271d light). The outline is an inset shadow like the
	   other variants', so a danger button is the same size as its neighbours. */
  .btn-danger {
    background: transparent;
    box-shadow: inset 0 0 0 1px var(--status-danger-fg);
    color: var(--status-danger-fg);
  }
  .btn-danger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--status-danger-fg) 12%, transparent);
  }
</style>
