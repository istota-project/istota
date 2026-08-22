<script lang="ts">
  /**
   * One row in the notification panel.
   *
   * Severity dot, title, relative time, an `×N` marker where the row has fired
   * more than once, and up to two inline actions. The row itself opens the
   * detail modal, which is where the full body and every action live.
   *
   * **Every field here is rendered as a text node.** `title` and `body` reach
   * this component off a stranger's mail — a gated email's sender and subject
   * are attacker-supplied — so there is no `{@html}` anywhere in this file and
   * none may be added.
   *
   * The row-opening control and the action buttons are **siblings**, not nested:
   * a `<button>` inside a `<button>` is invalid markup and the platform
   * resolves the click for you.
   */
  import { base } from '$app/paths';
  import type { NotificationAction, ResolvedNotification } from '$lib/api';
  import Button from './Button.svelte';

  interface Props {
    item: ResolvedNotification;
    /** Open the detail modal for this row. */
    onOpen: (item: ResolvedNotification) => void;
    onAction: (item: ResolvedNotification, action: NotificationAction) => void;
  }

  let { item, onOpen, onAction }: Props = $props();

  /** Two, because past that the row is a form rather than a line in a list.
   *  The rest are in the detail modal, which renders every action there is. */
  const INLINE_ACTIONS = 2;

  const actions = $derived((item.actions ?? []).slice(0, INLINE_ACTIONS));

  /** Severity `warning` maps to the `warn` token family — the store's vocabulary
   *  and the token roster spell it differently, and this is the one place that
   *  matters. */
  const severityClass = $derived(item.severity === 'warning' ? 'warn' : item.severity);

  const variants: Record<string, 'primary' | 'secondary' | 'danger'> = {
    primary: 'primary',
    default: 'secondary',
    danger: 'danger',
  };

  /** Relative time, coarse.
   *
   * Read off `updated_at` rather than `created_at`: a reopened row keeps the
   * date it was first seen and refreshes the other, and "40 days ago" on an
   * alert that fired an hour ago is the wrong answer to the question the panel
   * is being asked. Local to this component — `/admin`, `/location` and the
   * device card each carry their own copy of this arithmetic, and folding the
   * four into one helper is a cleanup of its own rather than part of this.
   */
  function relative(ts: string): string {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return '';
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }
</script>

<div class="notification-item" class:unseen={!item.seen_at}>
  <button
    type="button"
    class="item-open"
    onclick={() => onOpen(item)}
    aria-label="Open notification: {item.title}"
  >
    <span class="severity-dot severity-{severityClass}" aria-hidden="true"></span>
    <span class="item-text">
      <span class="item-title">{item.title}</span>
      <span class="item-meta caption">
        <span>{relative(item.updated_at)}</span>
        {#if item.occurrences > 1}
          <span class="occurrences" title="Raised {item.occurrences} times"
            >×{item.occurrences}</span
          >
        {/if}
      </span>
    </span>
  </button>
  {#if actions.length}
    <div class="item-actions">
      {#each actions as action (action.id)}
        {#if action.method === 'LINK' && action.href}
          <Button
            variant={variants[action.kind] ?? 'secondary'}
            size="sm"
            href="{base}{action.href}">{action.label}</Button
          >
        {:else if action.method === 'POST'}
          <Button
            variant={variants[action.kind] ?? 'secondary'}
            size="sm"
            onclick={() => onAction(item, action)}>{action.label}</Button
          >
        {/if}
      {/each}
    </div>
  {/if}
</div>

<style>
  .notification-item {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-2);
    border-radius: var(--radius-sm);
  }

  .notification-item:hover {
    background: var(--surface-raised);
  }

  .item-open {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    padding: 0;
    border: none;
    background: none;
    font: inherit;
    line-height: 1.4;
    text-align: left;
    cursor: pointer;
    color: inherit;
    /* The row is full-bleed inside its own padding, so the global focus ring
       would paint outside the item's box and over its neighbour. */
    outline-offset: calc(-1 * var(--focus-ring-width));
  }

  .severity-dot {
    flex: none;
    width: 0.5rem;
    height: 0.5rem;
    /* Optical alignment with the middle of the title's first line rather than
       its box top, which sits a step high at every text scale. Below --space-1
       and deliberately off the ramp: the smallest step is 0.25rem and the dot
       is 0.5rem tall, so a ramp value here is visibly wrong in one direction or
       the other. */
    /* design-lint-allow: sub---space-1 optical nudge, centring a 0.5rem dot on a line box */
    margin-top: 0.35rem;
    border-radius: 50%;
    background: var(--text-dim);
  }

  .severity-danger {
    background: var(--status-danger-fg);
  }
  .severity-warn {
    background: var(--status-warn-fg);
  }
  .severity-success {
    background: var(--status-success-fg);
  }
  .severity-info {
    background: var(--status-info-fg);
  }

  .item-text {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .item-title {
    color: var(--text-secondary);
    font-size: var(--text-sm);
    /* Two lines, then clip. A subject line has no bound and one long row would
       push every other item off the panel. */
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  /* An unseen row is the *only* thing weight is used for here, so it reads as
     new rather than as important — severity is the dot's job. */
  .unseen .item-title {
    color: var(--text-primary);
    font-weight: 600;
  }

  .item-meta {
    display: flex;
    gap: var(--space-2);
  }

  .occurrences {
    color: var(--text-muted);
  }

  .item-actions {
    flex: none;
    display: flex;
    gap: var(--space-1);
  }
</style>
