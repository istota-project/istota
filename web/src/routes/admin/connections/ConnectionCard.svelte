<script lang="ts">
  import type { Component, Snippet } from 'svelte';
  import { Badge, Card } from '$lib/components/ui';

  type BadgeVariant = 'success' | 'warn' | 'danger' | 'neutral' | 'info';

  interface Props {
    testid: string;
    title: string;
    /** One line under the title saying what the connection is for. */
    subtitle?: string;
    icon: Component<{ size?: number }>;
    badge?: { variant: BadgeVariant; label: string } | null;
    children: Snippet;
    /** The footer's left end: when the card was last checked, and how fresh that is. */
    meta?: Snippet;
    /** The footer's right end. */
    actions?: Snippet;
  }

  let { testid, title, subtitle, icon: Icon, badge, children, meta, actions }: Props = $props();

  const foot = $derived(meta || actions ? footSnippet : undefined);
</script>

<!--
  The shell every Connections card shares, so the four read as one set: an
  identifying head, a body, and a footer that is always the card's last row.
  The cards stack full width; the body is the grid, so a wide pane fits
  more of a card's sections side by side.
-->
<Card {title} description={subtitle} data-testid={testid} footer={foot}>
  {#snippet icon()}
    <span class="connection-icon" aria-hidden="true"><Icon size={18} /></span>
  {/snippet}
  {#snippet status()}
    {#if badge}
      <Badge variant={badge.variant}>{badge.label}</Badge>
    {/if}
  {/snippet}

  <div class="connection-body">
    {@render children()}
  </div>
</Card>

{#snippet footSnippet()}
  <div class="connection-foot">
    <div class="connection-meta">
      {#if meta}{@render meta()}{/if}
    </div>
    {#if actions}
      <div class="connection-actions">{@render actions()}</div>
    {/if}
  </div>
{/snippet}

<style>
  .connection-icon {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2.25rem;
    height: 2.25rem;
    border-radius: var(--radius-md);
    background: var(--surface-raised);
    color: var(--text-secondary);
  }

  /* The cards run full width, so the body packs its panels side by side as
     the card widens and stacks them on a phone. A caller marks a child
     `panel` for one column of the body and `wide` for a full-width row (a
     notice, a closing caption, a form). */
  .connection-body {
    flex: 1;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 22rem), 1fr));
    align-items: start;
    gap: var(--space-4) var(--space-6);
    min-width: 0;
  }

  .connection-body > :global(*) {
    min-width: 0;
    margin: 0;
  }

  .connection-body > :global(.wide) {
    grid-column: 1 / -1;
  }

  .connection-body > :global(.panel) {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .connection-body :global(.panel > h3) {
    margin: 0;
  }

  /* One label column across a card's panels, so the values start at the
     same inset rather than wherever each panel's longest label put them. */
  .connection-body :global(.kv) {
    grid-template-columns: minmax(7rem, max-content) minmax(0, 1fr);
  }

  @container settings (max-width: 30rem) {
    .connection-body :global(.kv) {
      grid-template-columns: max-content minmax(0, 1fr);
    }
  }

  .connection-body :global(dd) {
    margin: 0;
    overflow-wrap: anywhere;
    user-select: text;
  }

  /* Inside the Card footer's flush-right row, taking the whole of it so the
     meta keeps the left end. */
  .connection-foot {
    flex: 1 1 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
  }

  /* The footer's left end takes the room the buttons leave, and a long note
     wraps there at a readable measure rather than running under them. */
  .connection-meta {
    flex: 1 1 20rem;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
    max-width: 72ch;
  }

  .connection-meta :global(.caption) {
    margin: 0;
  }

  .connection-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-left: auto;
  }
</style>
