<script lang="ts">
  import { ArrowDown, ArrowUp, ArrowUpDown } from '@lucide/svelte';

  interface Props {
    label: string;
    /** Lowercase name read by a screen reader ("invoice number"). */
    name?: string;
    active: boolean;
    descending: boolean;
    /** Placement only: the page's column class. */
    class?: string;
    onclick: () => void;
  }

  let { label, name, active, descending, class: column = '', onclick }: Props = $props();

  const spoken = $derived(name ?? label.toLowerCase());
  const description = $derived(
    active
      ? `Sorted by ${spoken}, ${descending ? 'newest first' : 'oldest first'}. Reverse the order`
      : `Sort by ${spoken}`,
  );
</script>

<button
  type="button"
  class="money-sortable {column}"
  class:is-sorted={active}
  aria-label={description}
  title={description}
  {onclick}
>
  {label}<span class="money-sort-arrow" aria-hidden="true"
    >{#if !active}<ArrowUpDown size={12} />{:else if descending}<ArrowDown
        size={12}
      />{:else}<ArrowUp size={12} />{/if}</span
  >
</button>
