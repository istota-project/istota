<script lang="ts">
  import { DropdownMenu } from 'bits-ui';
  import { MoreVertical } from 'lucide-svelte';

  export interface KebabItem {
    label: string;
    onSelect: () => void;
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
        <DropdownMenu.Item
          class={item.danger ? 'ui-kebab-item ui-kebab-item--danger' : 'ui-kebab-item'}
          disabled={item.disabled}
          onSelect={item.onSelect}
        >
          {item.label}
        </DropdownMenu.Item>
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
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
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
  :global(.ui-kebab-item[data-highlighted]) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }
  :global(.ui-kebab-item[data-disabled]) {
    opacity: 0.4;
    cursor: not-allowed;
  }
  :global(.ui-kebab-item--danger[data-highlighted]) {
    color: #d46ab5;
  }

  /* Light theme overrides — dark rules above untouched. */
  :global(:root[data-theme='light']) :global(.ui-kebab-item--danger[data-highlighted]) {
    color: #a3157e;
  }
</style>
