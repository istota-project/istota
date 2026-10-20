<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    /** The list's heading. Leave it out when the groups inside name themselves. */
    title?: string;
    count?: number;
    open?: boolean;
    width?: string;
    onClose?: () => void;
    extras?: Snippet;
    children: Snippet;
  }

  let { title, count, open = false, width = '220px', onClose, extras, children }: Props = $props();
</script>

{#if open && onClose}
  <button class="sidebar-backdrop" aria-label="Close sidebar" onclick={onClose} type="button"
  ></button>
{/if}

<aside class="sidebar" class:open style="--sidebar-width: {width}">
  {#if title}
    <div class="sidebar-header">
      <span class="sidebar-title micro-label">{title}</span>
      {#if count !== undefined}<span class="sidebar-count caption num">{count}</span>{/if}
    </div>
  {/if}
  {#if extras}{@render extras()}{/if}
  <div class="sidebar-list" class:untitled={!title}>{@render children()}</div>
</aside>

<style>
  .sidebar {
    width: var(--sidebar-width);
    flex-shrink: 0;
    border-right: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .sidebar-header {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    padding: var(--space-3);
    flex-shrink: 0;
  }

  .sidebar-list.untitled {
    padding-top: var(--space-2);
  }

  .sidebar-list {
    flex: 1;
    min-width: 0;
    overflow-x: hidden;
    overflow-y: auto;
    padding: 0 var(--space-1) var(--space-2);
  }

  .sidebar-backdrop {
    display: none;
  }

  @media (max-width: 768px) {
    /* The drawer slides on transform alone, so the compositor runs the whole
       animation without layout or paint. `visibility` waits out the slide on
       close and keeps the closed drawer out of the tab order and the
       accessibility tree, which `display: none` used to do. */
    .sidebar {
      position: absolute;
      top: 0;
      left: 0;
      bottom: 0;
      z-index: var(--z-drawer);
      width: 220px;
      background: var(--surface-base);
      border-right: 1px solid var(--border-default);
      transform: translate3d(-100%, 0, 0);
      visibility: hidden;
      will-change: transform;
      transition:
        transform var(--duration-fast) var(--ease-decelerate),
        visibility 0s linear var(--duration-fast);
    }

    .sidebar.open {
      transform: translate3d(0, 0, 0);
      visibility: visible;
      transition:
        transform var(--duration-slow) var(--ease-decelerate),
        visibility 0s;
    }

    /* The drawer is positioned against the shell body, so it sits outside the
		   scroll area's bottom inset and its last row would land in the
		   home-indicator strip. (Horizontal insets come from .shell.) */
    .sidebar-list {
      padding-bottom: max(0.5rem, var(--safe-bottom));
    }

    .sidebar-backdrop {
      display: block;
      position: fixed;
      inset: 0;
      background: transparent;
      border: none;
      padding: 0;
      cursor: default;
      z-index: var(--z-drawer-backdrop);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .sidebar,
    .sidebar.open {
      transition: none;
    }
  }
</style>
