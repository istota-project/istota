<script lang="ts">
  import type { Snippet } from 'svelte';

  type Variant = 'primary' | 'secondary' | 'ghost' | 'pill' | 'subtle' | 'danger' | 'danger-icon';
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
    onclick,
    title,
    disabled,
    loading = false,
    loadingLabel = 'Saving…',
    ariaLabel,
    children,
  }: Props = $props();
</script>

{#if href}
  <!-- A disabled link is not a thing the platform has, so a disabled href
       renders as a real disabled button instead of an inert-looking anchor
       that still navigates. -->
  {#if disabled || loading}
    <button class="btn btn-{variant} btn-{size}" type="button" disabled aria-label={ariaLabel}>
      {#if loading}{loadingLabel}{:else}{@render children()}{/if}
    </button>
  {:else}
    <a
      class="btn btn-{variant} btn-{size}"
      {href}
      {download}
      {target}
      {rel}
      {title}
      {onclick}
      aria-label={ariaLabel}
    >
      {@render children()}
    </a>
  {/if}
{:else}
  <button
    class="btn btn-{variant} btn-{size}"
    {type}
    {onclick}
    {title}
    disabled={disabled || loading}
    aria-busy={loading || undefined}
    aria-label={ariaLabel}
  >
    {#if loading}{loadingLabel}{:else}{@render children()}{/if}
  </button>
{/if}

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
    line-height: 1.2;
    border-radius: var(--control-radius);
    cursor: pointer;
    transition: all var(--transition-fast);
    user-select: none;
  }

  .btn:disabled {
    opacity: 0.5;
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
	   a pair (Disconnect next to Connect), where a ghost button would read as a
	   different kind of control rather than a calmer one. Sits on --surface-card
	   surfaces, so it can't use btn-pill's matching background. */
  .btn-secondary {
    background: var(--surface-raised);
    color: var(--text-muted);
  }
  .btn-secondary:hover:not(:disabled) {
    background: var(--border-default);
    color: var(--text-primary);
  }

  .btn-pill {
    background: var(--surface-card);
    color: var(--text-muted);
  }
  .btn-pill:hover:not(:disabled) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  .btn-ghost {
    background: transparent;
    color: var(--text-muted);
  }
  .btn-ghost:hover:not(:disabled) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  .btn-subtle {
    background: transparent;
    color: var(--text-dim);
    padding-inline: var(--space-2);
  }
  .btn-subtle:hover:not(:disabled) {
    color: var(--text-muted);
  }

  /* Red outline/text for a destructive confirm action (the confirm button in
	   ConfirmDialog). Matches the hand-rolled .btn.danger convention it replaces
	   (#c66 dark / #c0271d light). */
  .btn-danger {
    background: transparent;
    border: 1px solid var(--status-danger-fg);
    color: var(--status-danger-fg);
  }
  .btn-danger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--status-danger-fg) 12%, transparent);
  }

  .btn-danger-icon {
    background: transparent;
    color: var(--text-dim);
    padding: 0.2rem var(--space-2);
  }
  .btn-danger-icon:hover:not(:disabled) {
    color: var(--status-danger-fg);
  }
</style>
