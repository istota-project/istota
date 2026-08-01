<script lang="ts">
  import type { Snippet } from 'svelte';

  /**
   * Full-width notice / alert banner for the top of a page.
   *
   * Renders as a single line (the `title`) with a variant-colored left border.
   * When `children` are supplied it is expandable — clicking the header toggles
   * the detail body. `collapsed` is bindable, defaulting to true so the banner
   * starts as one line. With no `children` it's a static one-line notice (no
   * toggle).
   */
  type Variant = 'info' | 'warn' | 'danger';

  interface Props {
    title: string;
    variant?: Variant;
    collapsed?: boolean;
    children?: Snippet;
  }

  let { title, variant = 'warn', collapsed = $bindable(true), children }: Props = $props();

  const expandable = $derived(!!children);
</script>

<section class="notice-banner notice-{variant}" role="note">
  {#if expandable}
    <button
      type="button"
      class="notice-head"
      onclick={() => (collapsed = !collapsed)}
      aria-expanded={!collapsed}
    >
      <span class="notice-title">{title}</span>
      <span class="notice-toggle">{collapsed ? '▸' : '▾'}</span>
    </button>
    {#if !collapsed}
      <div class="notice-body">{@render children?.()}</div>
    {/if}
  {:else}
    <div class="notice-head notice-static">
      <span class="notice-title">{title}</span>
    </div>
  {/if}
</section>

<style>
  .notice-banner {
    width: 100%;
    background: var(--surface-card);
    border: 1px solid var(--border-subtle);
    border-left: 3px solid var(--notice-accent);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
  }

  .notice-warn {
    --notice-accent: var(--accent-amber);
  }
  .notice-danger {
    --notice-accent: var(--status-danger-fg);
  }
  .notice-info {
    --notice-accent: var(--status-info-fg);
  }

  .notice-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    width: 100%;
    background: none;
    border: none;
    padding: 0;
    margin: 0;
    cursor: pointer;
    color: inherit;
    font: inherit;
    text-align: left;
  }

  .notice-static {
    cursor: default;
  }

  .notice-title {
    font-weight: 600;
    font-size: 0.9rem;
  }

  .notice-toggle {
    opacity: 0.6;
    font-size: 0.85rem;
    flex-shrink: 0;
  }

  .notice-body {
    margin-top: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
</style>
