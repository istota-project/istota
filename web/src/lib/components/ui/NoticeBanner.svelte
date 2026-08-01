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

  /* The banner's two type sizes are the component's, not the caller's: a title
     at --text-base over a body at --text-sm. Both were raw literals (0.9rem /
     0.85rem), which is what let a caller "match" the banner by writing its own
     approximation of them — the admin standalone notice sized its whole slot in
     0.9/0.85 and so rendered a step larger than every other banner in the app.
     Keep them tokens, and keep them here. */
  .notice-title {
    font-weight: 600;
    font-size: var(--text-base);
  }

  .notice-toggle {
    opacity: 0.6;
    /* Same size as the title: the caret shares its flex row, so a step down
       would set the header's line box from the glyph rather than the text. */
    font-size: var(--text-base);
    flex-shrink: 0;
  }

  .notice-body {
    margin-top: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  /* Slot content inherits the body size. A caller styling its own headings and
     labels inside the slot restates weight and colour, never size — the whole
     point of the banner being one component is that its body is one size. */
  .notice-body :global(p) {
    margin: 0 0 var(--space-2);
  }

  .notice-body :global(p:last-child) {
    margin-bottom: 0;
  }
</style>
