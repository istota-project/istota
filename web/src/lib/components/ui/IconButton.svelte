<script lang="ts">
  import type { Snippet } from 'svelte';

  type Size = 'sm' | 'md' | 'round';

  interface Props {
    /**
     * Required, and becomes the aria-label. An icon-only button has no text to
     * name it, and 146 bare <button>s in this tree relied on the author
     * remembering to add one — several did not.
     */
    label: string;
    size?: Size;
    /** Hover turns the glyph red rather than raising it. For a delete action. */
    danger?: boolean;
    /** Pressed/open state, for a menu trigger. */
    active?: boolean;
    type?: 'button' | 'submit';
    title?: string;
    disabled?: boolean;
    onclick?: (e: MouseEvent) => void;
    children: Snippet;
  }

  let {
    label,
    size = 'md',
    danger = false,
    active = false,
    type = 'button',
    title,
    disabled,
    onclick,
    children,
  }: Props = $props();
</script>

<button
  class="icon-btn icon-btn-{size}"
  class:danger
  class:active
  {type}
  {title}
  {disabled}
  {onclick}
  aria-label={label}
>
  {@render children()}
</button>

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

  .icon-btn:hover:not(:disabled),
  .icon-btn.active {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  .icon-btn.danger:hover:not(:disabled) {
    background: transparent;
    color: var(--status-danger-fg);
  }

  .icon-btn:disabled {
    opacity: 0.3;
    cursor: not-allowed;
  }

  .icon-btn-sm {
    padding: 0.15rem 0.35rem;
    border-radius: var(--radius-sm);
  }

  .icon-btn-md {
    padding: 0.3rem;
    border-radius: var(--radius-sm);
  }

  /* A circular target sized in em so it tracks the text scale — the composer's
     mic and send, where the control sits in a row with the text it belongs to. */
  .icon-btn-round {
    flex-shrink: 0;
    width: 2.35em;
    height: 2.35em;
    border-radius: 50%;
  }

  .icon-btn-round :global(svg) {
    width: 1.25em;
    height: 1.25em;
  }
</style>
