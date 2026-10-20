<script lang="ts" module>
  import type { MenuItem } from './Menu.svelte';

  export type KebabItem = MenuItem;
</script>

<script lang="ts">
  import Icon from './Icon.svelte';
  import { MoreVertical } from '@lucide/svelte';
  import Menu from './Menu.svelte';

  interface Props {
    items: KebabItem[];
    ariaLabel?: string;
    // The trigger's glyph. The kebab stays the default for row and card
    // actions; a menu holding one kind of action (Download) names it.
    icon?: typeof MoreVertical;
  }

  let { items, ariaLabel = 'Actions', icon: Glyph = MoreVertical }: Props = $props();
</script>

<!-- `Menu` with the kebab trigger: the row and card actions preset. -->
<Menu {items} label={ariaLabel}>
  {#snippet trigger(props)}
    <button
      type="button"
      {...props}
      class="ui-kebab-trigger touch-target"
      aria-label={ariaLabel}
      onclick={(e) => {
        // A row that opens on click must not also open under its own kebab.
        e.stopPropagation();
        if (typeof props.onclick === 'function') props.onclick(e);
      }}
    >
      <Icon icon={Glyph} size="md" />
    </button>
  {/snippet}
</Menu>

<style>
  /* Global, as before the move onto Menu: table.css and CredentialsCard place
     the trigger from outside at one class's weight. */
  :global(.ui-kebab-trigger) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    color: var(--text-dim);
    cursor: pointer;
    padding: 0 0.1rem;
    line-height: var(--leading-none);
    border-radius: var(--radius-sm);
    flex-shrink: 0;
    transition: color var(--transition-fast);
  }
  :global(.ui-kebab-trigger:hover),
  :global(.ui-kebab-trigger[data-state='open']) {
    color: var(--text-primary);
  }
  @media (pointer: coarse) {
    :global(.ui-kebab-trigger) {
      min-width: var(--touch-min);
    }
  }
</style>
