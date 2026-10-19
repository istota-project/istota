<script lang="ts" module>
  import type { Component, Snippet } from 'svelte';
  import type { NavItem } from './HeaderNav.svelte';

  export interface SidebarNavItem extends NavItem {
    /** A lucide component, drawn before the label. */
    icon?: Component<{ size?: number | string; 'aria-hidden'?: boolean | 'true' | 'false' }>;
    /** A `CountPill` at the row's end; nothing at zero. */
    count?: number;
    /** The pill's tooltip noun: "unread" titles it "5 unread". */
    countLabel?: string;
    countTone?: 'accent' | 'muted';
    /** Straight after the label: a state glyph. */
    mark?: Snippet;
  }
</script>

<script lang="ts">
  import CountPill from './CountPill.svelte';
  import ListRow from './ListRow.svelte';

  interface Props {
    items: SidebarNavItem[];
    /** The nav landmark's accessible name. */
    label: string;
    /** Called on every click with the item, before the browser follows the link:
     *  close a drawer, or `preventDefault()` and push a selection instead. */
    onnavigate?: (item: SidebarNavItem, event: MouseEvent) => void;
    /** A rule under the block, separating it from a list below it. */
    divider?: boolean;
    density?: 'base' | 'sm';
  }

  let { items, label, onnavigate, divider = false, density = 'base' }: Props = $props();
</script>

<nav class="ui-sidebar-nav" class:divider aria-label={label}>
  {#each items as item (item.href)}
    {#snippet icon()}{#if item.icon}<item.icon size={14} aria-hidden="true" />{/if}{/snippet}
    {#snippet badge()}<CountPill
        count={item.count ?? 0}
        tone={item.countTone}
        title={item.countLabel ? `${item.count} ${item.countLabel}` : undefined}
      />{/snippet}
    <ListRow
      href={item.href}
      active={item.active}
      {density}
      icon={item.icon ? icon : undefined}
      mark={item.mark}
      badge={item.count ? badge : undefined}
      onclick={onnavigate ? (event) => onnavigate(item, event) : undefined}
    >
      {item.label}
    </ListRow>
  {/each}
</nav>

<style>
  .ui-sidebar-nav {
    display: flex;
    flex-direction: column;
  }

  .ui-sidebar-nav.divider {
    margin-bottom: var(--space-2);
    padding-bottom: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
</style>
