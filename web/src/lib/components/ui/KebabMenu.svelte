<script lang="ts">
  import { DropdownMenu } from 'bits-ui';
  import { MoreVertical } from 'lucide-svelte';

  export interface KebabItem {
    label: string;
    // Omit when the item is a link (`href`). One of the two must be set.
    onSelect?: () => void;
    // Renders the item as a real anchor so navigation keeps middle-click,
    // open-in-new-tab and the status-bar URL preview. A menu item that only
    // navigates should use this rather than an onSelect + goto().
    href?: string;
    danger?: boolean;
    disabled?: boolean;
  }

  interface Props {
    items: KebabItem[];
    ariaLabel?: string;
  }

  let { items, ariaLabel = 'Actions' }: Props = $props();
</script>

<DropdownMenu.Root>
  <DropdownMenu.Trigger
    class="ui-kebab-trigger"
    aria-label={ariaLabel}
    onclick={(e) => e.stopPropagation()}
  >
    <MoreVertical size={15} />
  </DropdownMenu.Trigger>
  <DropdownMenu.Portal>
    <DropdownMenu.Content class="ui-kebab-content" sideOffset={4} align="end">
      {#each items as item (item.label)}
        {@const cls = item.danger ? 'ui-kebab-item ui-kebab-item--danger' : 'ui-kebab-item'}
        {#if item.href}
          <DropdownMenu.Item class={cls} disabled={item.disabled}>
            {#snippet child({ props })}
              <a {...props} href={item.href}>{item.label}</a>
            {/snippet}
          </DropdownMenu.Item>
        {:else}
          <DropdownMenu.Item class={cls} disabled={item.disabled} onSelect={item.onSelect}>
            {item.label}
          </DropdownMenu.Item>
        {/if}
      {/each}
    </DropdownMenu.Content>
  </DropdownMenu.Portal>
</DropdownMenu.Root>

<style>
  :global(.ui-kebab-trigger) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    color: var(--text-dim);
    cursor: pointer;
    padding: 0 0.1rem;
    line-height: 1;
    border-radius: 0.25rem;
    flex-shrink: 0;
    transition: color var(--transition-fast);
  }
  :global(.ui-kebab-trigger:hover),
  :global(.ui-kebab-trigger[data-state='open']) {
    color: var(--text-primary);
  }

  :global(.ui-kebab-content) {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: 0.4rem;
    padding: 0.25rem;
    z-index: 100;
    box-shadow: var(--shadow-md);
    min-width: 9rem;
    outline: none;
  }

  :global(.ui-kebab-item) {
    padding: 0.35rem 0.6rem;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    border-radius: 0.3rem;
    cursor: pointer;
    outline: none;
    user-select: none;
    white-space: nowrap;
  }
  /* An href item renders as an anchor; strip link chrome and let it fill the
	   row so the hit area matches a button item exactly. */
  :global(a.ui-kebab-item) {
    display: block;
    text-decoration: none;
    color: inherit;
  }

  :global(.ui-kebab-item[data-highlighted]) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }
  :global(.ui-kebab-item[data-disabled]) {
    opacity: 0.4;
    cursor: not-allowed;
  }
  :global(.ui-kebab-item--danger[data-highlighted]) {
    color: var(--status-danger-fg);
  }
</style>
