<script lang="ts">
  import Menu from './Menu.svelte';
  import type { MenuItem } from './Menu.svelte';

  let {
    items,
    retainFocus = false,
    trailing = false,
  }: { items: MenuItem[]; retainFocus?: boolean; trailing?: boolean } = $props();

  const shown = $derived(
    trailing ? items.map((item, i) => (i === 0 ? { ...item, trailing: count } : item)) : items,
  );
</script>

{#snippet count()}<span class="test-count">3</span>{/snippet}

<!-- A field that keeps focus, standing in for the chat composer's textarea. -->
<textarea data-testid="field"></textarea>
<Menu items={shown} label="Row actions" {retainFocus}>
  {#snippet trigger(props)}
    <button type="button" {...props}>Open</button>
  {/snippet}
</Menu>
