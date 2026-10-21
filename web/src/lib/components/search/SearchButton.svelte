<script lang="ts">
  import { Icon } from '@minot/ui';
  import { Search } from '@lucide/svelte';
  let { onclick }: { onclick: () => void } = $props();
</script>

<button
  type="button"
  class="nav-icon-btn touch-target search-btn"
  aria-label="Search"
  aria-keyshortcuts="Meta+K Control+K"
  title="Search"
  {onclick}
>
  <Icon icon={Search} size="md" />
</button>

<style>
  .search-btn {
    color: var(--text-dim);
  }
</style>
