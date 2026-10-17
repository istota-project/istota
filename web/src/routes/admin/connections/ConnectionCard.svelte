<script lang="ts">
  import type { Component, Snippet } from 'svelte';
  import { Badge } from '$lib/components/ui';

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
</script>

<!--
  The shell every Connections card shares, so the four read as one set: an
  identifying head, a body, and a footer that is always the card's last row.
  The footer sits at the bottom of the card even when a neighbour in the same
  grid row is taller, so action rows line up across the row.
-->
<section class="card connection" data-testid={testid}>
  <header class="connection-head">
    <span class="connection-icon" aria-hidden="true"><Icon size={18} /></span>
    <div class="connection-title">
      <h2>{title}</h2>
      {#if subtitle}
        <p class="caption">{subtitle}</p>
      {/if}
    </div>
    {#if badge}
      <Badge variant={badge.variant}>{badge.label}</Badge>
    {/if}
  </header>

  <div class="connection-body">
    {@render children()}
  </div>

  {#if meta || actions}
    <footer class="connection-foot">
      <div class="connection-meta">
        {#if meta}{@render meta()}{/if}
      </div>
      {#if actions}
        <div class="connection-actions">{@render actions()}</div>
      {/if}
    </footer>
  {/if}
</section>

<style>
  .connection {
    min-width: 0;
  }

  .connection-head {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
  }

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

  .connection-title {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .connection-title .caption {
    margin: 0;
  }

  .connection-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }

  /* One label column for every card, so the values start at the same inset
     down the page rather than wherever each card's longest label put them. */
  .connection-body :global(.kv) {
    grid-template-columns: minmax(8rem, max-content) minmax(0, 1fr);
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

  .connection-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
  }

  .connection-meta {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
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
