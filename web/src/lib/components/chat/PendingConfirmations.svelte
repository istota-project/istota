<script lang="ts">
  /**
   * Questions waiting on the user that no transcript can show them.
   *
   * The in-room `ConfirmationCard` covers a turn the user started here. This
   * covers the one they did not: an inbound email held by the untrusted-sender
   * gate (ISSUE-241). Two things make it its own surface rather than a card in
   * a room — a first-contact email's conversation token is a synthetic thread
   * hash belonging to no room, and its body is withheld until the answer, so
   * there is nothing in the transcript to hang a card on.
   *
   * Everything rendered here is a text node. The sender and subject come off
   * the wire from a stranger.
   */
  import { Button } from '$lib/components/ui';
  import type { PendingConfirmation } from '$lib/api';

  let {
    items,
    onAnswer,
  }: {
    items: PendingConfirmation[];
    onAnswer: (taskId: number, approve: boolean) => void | Promise<void>;
  } = $props();

  // Per-card, so answering one does not freeze the others.
  let busy = $state<Set<number>>(new Set());

  async function answer(taskId: number, approve: boolean) {
    if (busy.has(taskId)) return;
    busy = new Set(busy).add(taskId);
    try {
      await onAnswer(taskId, approve);
    } finally {
      const next = new Set(busy);
      next.delete(taskId);
      busy = next;
    }
  }
</script>

{#if items.length > 0}
  <section class="pending" aria-label="Waiting for your confirmation">
    {#each items as item (item.task_id)}
      <article class="pending-card">
        <div class="pending-body">
          <span class="micro-label">Waiting for you</span>
          {#if item.email}
            <p class="pending-line">
              Email from <strong>{item.email.sender}</strong>
            </p>
            <p class="caption">
              {item.email.subject || '(no subject)'}
              {#if item.email.routing_method}
                &middot; routed via {item.email.routing_method}
              {/if}
            </p>
          {:else}
            <p class="pending-line">{item.summary}</p>
          {/if}
          <p class="caption">Task #{item.task_id}</p>
        </div>
        <div class="pending-actions">
          <Button
            variant="primary"
            size="sm"
            disabled={busy.has(item.task_id)}
            onclick={() => answer(item.task_id, true)}>Confirm</Button
          >
          <Button
            variant="subtle"
            size="sm"
            disabled={busy.has(item.task_id)}
            onclick={() => answer(item.task_id, false)}>Discard</Button
          >
        </div>
      </article>
    {/each}
  </section>
{/if}

<style>
  .pending {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
  }
  .pending-card {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-left: 3px solid var(--status-warn-fg);
    border-radius: var(--radius-card);
    padding: var(--space-2) var(--space-3);
  }
  .pending-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }
  .pending-line {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }
  .pending-body :global(p.caption) {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .pending-actions {
    display: flex;
    gap: var(--space-2);
  }
</style>
