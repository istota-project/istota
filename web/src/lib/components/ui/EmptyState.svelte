<svelte:options runes={true} />

<script lang="ts">
  import type { Snippet } from 'svelte';
  import Button from './Button.svelte';

  interface Props {
    /**
     * `md` for a page, a pane or a card's whole body; `sm` for a sub-list
     * inside a card, where the full padding would outweigh its neighbours.
     */
    size?: 'md' | 'sm';
    /** A heading above the message, for a page whose whole body is this state. */
    title?: string;
    /** The one thing to do about it, as a centred button under the message. */
    actionLabel?: string;
    /** The action navigates: rendered as a link, so it keeps open-in-new-tab. */
    actionHref?: string;
    /** The action stays on the page (opens a form, a dialog). */
    onaction?: () => void;
    children: Snippet;
  }

  let { size = 'md', title, actionLabel, actionHref, onaction, children }: Props = $props();
</script>

<div class="empty-state" class:small={size === 'sm'}>
  {#if title}<h2 class="title">{title}</h2>{/if}
  <p class="message">{@render children()}</p>
  {#if actionLabel && (actionHref || onaction)}
    <Button variant="primary" href={actionHref} onclick={onaction}>{actionLabel}</Button>
  {/if}
</div>

<style>
  /* Top of its slot rather than centred in it: a pane's empty state sits where
     the first row would have been, under whatever toolbar is still showing.
     `.empty-state` is a public hook: a frame that pads its content drops its
     top padding for one (`.frame:has(> :global(.empty-state))`), so every
     empty state lands the same distance below its bar. */
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-8) var(--space-4);
    text-align: center;
    color: var(--text-dim);
    font-size: var(--text-base);
  }

  .empty-state.small {
    gap: var(--space-2);
    padding: var(--space-3) 0;
    font-size: var(--text-sm);
  }

  /* Same size as the message: weight and colour set it apart, not a step up. */
  .title {
    margin: 0 0 calc(-1 * var(--space-2));
    font-size: inherit;
    font-weight: var(--weight-semibold);
    color: var(--text-primary);
  }

  .message {
    margin: 0;
    max-width: 40rem;
  }
</style>
