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
  }

  let { checked = false, icon = false, onclick, children, title, ...rest }: Props = $props();
</script>

<button class="chip" class:checked class:icon {onclick} {title} type="button" {...rest}
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
    line-height: 1.2;
    transition: all var(--transition-fast);
    user-select: none;
    cursor: pointer;
    color: var(--text-muted);
    background: var(--surface-card);
  }

  .chip.icon {
    padding: var(--space-1);
  }

  .chip:hover {
    color: var(--text-primary);
    background: var(--surface-raised);
  }

  .chip.checked {
    background: var(--surface-raised);
    color: var(--text-primary);
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
