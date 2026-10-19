<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    checked?: boolean;
    icon?: boolean;
    onclick?: () => void;
    children?: Snippet;
    title?: string;
    /**
     * Declared because the component already forwards it and only the type
     * refused: `checked` draws the on state and says nothing to a screen
     * reader, so a chip used as a toggle rather than as a label needs this to
     * report the same thing the fill does.
     */
    'aria-pressed'?: boolean;
    /** A toggle the user may not change here: a field the deployment owns. */
    disabled?: boolean;
  }

  let {
    checked = false,
    icon = false,
    onclick,
    children,
    title,
    disabled = false,
    ...rest
  }: Props = $props();
</script>

<button class="chip" class:checked class:icon {onclick} {title} {disabled} type="button" {...rest}
  >{#if children}{@render children()}{/if}</button
>

<style>
  .chip {
    display: inline-flex;
    min-height: var(--control-height-sm);
    align-items: center;
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

  .chip:hover:not(:disabled) {
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

  @media (max-width: 640px) {
    .chip {
      padding: 0.2rem var(--space-2);
    }
    .chip.icon {
      padding: var(--space-1);
    }
  }
</style>
