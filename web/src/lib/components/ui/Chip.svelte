<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import { X } from '@lucide/svelte';

  interface Props extends Omit<HTMLButtonAttributes, 'children' | 'aria-pressed'> {
    checked?: boolean;
    icon?: boolean;
    /** `xs` is the compact chip: `--text-xs`, a hairline of vertical padding. */
    size?: 'sm' | 'xs';
    /**
     * A chip that stands for one item of a list the user is building (a sender
     * pattern, a staged condition): it draws a trailing remove button and is
     * a label rather than a button itself, since a button cannot hold one.
     */
    removable?: boolean;
    onremove?: () => void;
    /** The remove button's name; say what goes ("Remove news@example.com"). */
    removeLabel?: string;
    onclick?: (e: MouseEvent) => void;
    children?: Snippet;
    title?: string;
    /**
     * Declared because the component already forwards it and only the type
     * refused: `checked` draws the on state and says nothing to a screen
     * reader, so a chip used as a toggle rather than as a label needs this to
     * report the same thing the fill does.
     */
    'aria-pressed'?: boolean | 'true' | 'false' | 'mixed';
    /** A toggle the user may not change here: a field the deployment owns. */
    disabled?: boolean;
    class?: string;
  }

  let {
    checked = false,
    icon = false,
    size = 'sm',
    removable = false,
    onremove,
    removeLabel = 'Remove',
    onclick,
    children,
    title,
    disabled = false,
    class: className = '',
    ...rest
  }: Props = $props();
</script>

{#if removable}
  <span class="chip removable {className}" class:xs={size === 'xs'} {title}
    >{#if children}<span class="chip-label">{@render children()}</span>{/if}<button
      type="button"
      class="chip-remove touch-target"
      aria-label={removeLabel}
      title={removeLabel}
      {disabled}
      onclick={onremove}><X size="1em" aria-hidden="true" /></button
    ></span
  >
{:else}
  <button
    class="chip {className}"
    class:checked
    class:icon
    class:xs={size === 'xs'}
    {onclick}
    {title}
    {disabled}
    type="button"
    {...rest}
    >{#if children}{@render children()}{/if}</button
  >
{/if}

<style>
  .chip {
    display: inline-flex;
    min-height: var(--control-height-sm);
    align-items: center;
    gap: var(--space-1);
    padding: 0.15rem var(--chip-padding-x);
    border: none;
    border-radius: var(--control-radius);
    font-size: var(--text-sm);
    font-family: inherit;
    line-height: var(--leading-tight);
    transition: all var(--transition-fast);
    user-select: none;
    cursor: pointer;
    color: var(--text-muted);
    /* Outlined at rest: a --surface-card fill matched the card or dialog it
       sat on and left the label looking like plain text. */
    background: transparent;
    box-shadow: inset 0 0 0 1px var(--border-default);
  }

  .chip.icon {
    padding: var(--space-1);
  }

  .chip:hover:not(:disabled):not(.removable) {
    color: var(--text-primary);
    box-shadow: inset 0 0 0 1px var(--border-hover);
  }

  .chip.checked {
    background: var(--surface-raised);
    box-shadow: inset 0 0 0 1px var(--border-hover);
    color: var(--text-primary);
  }

  /* Dimmed rather than faded out: a toggle the deployment owns still shows
     its outline and its state. */
  .chip:disabled {
    opacity: 0.65;
    cursor: not-allowed;
  }

  /* The compact chip four hand-rolled buttons shared (room links, the model
     badge, jump-to, an active filter): the type a step down, no tier height. */
  .chip.xs {
    min-height: 0;
    /* design-lint-allow: the compact chip's hairline padding, below --space-1. */
    padding: 0.15rem var(--space-2);
    font-size: var(--text-xs);
  }

  .chip.removable {
    cursor: default;
    max-width: 100%;
    min-width: 0;
    color: var(--text-secondary);
  }
  .chip.removable.xs {
    padding-right: var(--space-1);
  }
  .chip-label {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .chip-remove {
    display: inline-flex;
    align-items: center;
    flex: none;
    padding: 0;
    border: none;
    border-radius: var(--radius-round);
    background: none;
    color: var(--text-muted);
    font: inherit;
    line-height: var(--leading-none);
    cursor: pointer;
  }
  .chip-remove:hover:not(:disabled) {
    color: var(--text-primary);
  }
  .chip-remove:disabled {
    cursor: not-allowed;
  }

  @media (max-width: 640px) {
    .chip:not(.xs) {
      padding: 0.2rem var(--space-2);
    }
    .chip.icon {
      padding: var(--space-1);
    }
  }
</style>
