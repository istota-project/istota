<!--
  A notice drawn into the page, for a condition that lasts while the page is
  open: access lost, a managed setting, a file with problems. Two appearances
  of one component. `bordered` is a card with a variant-coloured left border and
  a title over a body; `inline` is the small tinted `.banner` box. Transient
  feedback is `notify()`, not this.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { ChevronRight } from '@lucide/svelte';
  import Icon from './Icon.svelte';

  type Variant = 'info' | 'warn' | 'danger' | 'success';

  interface Props {
    variant?: Variant;
    title?: string;
    appearance?: 'bordered' | 'inline';
    /** Bindable. Passing it makes a titled alert with a body collapsible:
     *  the title becomes the toggle. Left unset, the body always shows. */
    collapsed?: boolean;
    /** Defaults to `note` when bordered and to none when inline. `alert` or
     *  `status` for an alert that appears in answer to something just done. */
    role?: 'note' | 'alert' | 'status';
    /** Buttons at the end of the title row (bordered) or after the body (inline). */
    actions?: Snippet;
    /** Placement only. */
    class?: string;
    children?: Snippet;
  }

  let {
    variant = 'warn',
    title,
    appearance = 'bordered',
    collapsed = $bindable(),
    role,
    actions,
    class: className = '',
    children,
  }: Props = $props();

  const collapsible = $derived(collapsed !== undefined && !!title && !!children);
  const showBody = $derived(!!children && !(collapsible && collapsed));
  // Bordered takes the section-title step; inline keeps the box's one size.
  const titleClass = $derived(
    appearance === 'bordered' ? 'ui-alert-title title-section' : 'ui-alert-title',
  );
  const resolvedRole = $derived(role ?? (appearance === 'bordered' ? 'note' : undefined));
</script>

<div
  class="ui-alert {appearance === 'inline'
    ? `ui-alert-inline banner ${variant}`
    : `ui-alert-bordered alert-${variant}`} {className}"
  role={resolvedRole}
>
  {#if title || (actions && appearance === 'bordered')}
    <div class="ui-alert-head">
      {#if collapsible}
        <button
          type="button"
          class="ui-alert-toggle button-reset"
          onclick={() => (collapsed = !collapsed)}
          aria-expanded={!collapsed}
        >
          <span class={titleClass}>{title}</span>
          <span class="ui-alert-marker" class:open={!collapsed}
            ><Icon icon={ChevronRight} size="xs" /></span
          >
        </button>
      {:else if title}
        <span class={titleClass}>{title}</span>
      {/if}
      {#if actions && appearance === 'bordered'}
        <span class="ui-alert-actions">{@render actions()}</span>
      {/if}
    </div>
  {/if}
  {#if showBody}
    <div class="ui-alert-body">{@render children?.()}</div>
  {/if}
  {#if actions && appearance === 'inline'}
    <div class="ui-alert-actions">{@render actions()}</div>
  {/if}
</div>

<style>
  .ui-alert-bordered {
    width: 100%;
    background: var(--surface-card);
    border: 1px solid var(--border-subtle);
    border-left: 3px solid var(--alert-accent);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
  }

  .alert-warn {
    --alert-accent: var(--accent-amber);
  }
  .alert-danger {
    --alert-accent: var(--status-danger-fg);
  }
  .alert-info {
    --alert-accent: var(--status-info-fg);
  }
  .alert-success {
    --alert-accent: var(--status-success-fg);
  }

  .ui-alert-head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .ui-alert-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    flex: 1;
    min-width: 0;
    text-align: left;
  }

  .ui-alert-head > .ui-alert-title {
    flex: 1;
    min-width: 0;
  }

  /* The type sizes are the component's, never the caller's. Bordered: the title
     at --text-base (the global .title-section) over a body at --text-sm. Both
     were once raw literals, which let a caller "match" the banner by writing its
     own approximation; the admin standalone notice rendered a step larger than
     every other banner that way. Inline: one size, the .banner box's. */
  .ui-alert-marker {
    opacity: 0.6;
    /* Same size as the title: the marker shares its flex row, so a step down
       would set the header's line box from the glyph rather than the text. */
    font-size: var(--text-base);
    flex-shrink: 0;
    display: inline-flex;
    transition: transform var(--transition-fast);
  }
  .ui-alert-marker.open {
    transform: rotate(90deg);
  }

  .ui-alert-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .ui-alert-bordered .ui-alert-body {
    margin-top: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
  .ui-alert-bordered > .ui-alert-body:first-child {
    margin-top: 0;
  }

  /* Inline: the title is a bold lead line in the box's own size and colour. */
  .ui-alert-inline .ui-alert-title {
    font-weight: var(--weight-semibold);
  }
  .ui-alert-inline .ui-alert-head + .ui-alert-body {
    margin-top: var(--space-1);
  }
  .ui-alert-inline > .ui-alert-actions {
    margin-top: var(--space-2);
  }

  /* Slot content inherits the body size. A caller styling its own headings and
     labels inside the slot restates weight and colour, never size. */
  .ui-alert-body :global(p) {
    margin: 0 0 var(--space-2);
  }

  .ui-alert-body :global(p:last-child) {
    margin-bottom: 0;
  }

  @media (prefers-reduced-motion: reduce) {
    .ui-alert-marker {
      transition: none;
    }
  }
</style>
