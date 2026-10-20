<!--
  Previous and Next over a list that is read a page at a time, with the page
  it is on between them. Nothing when there is only one page.
-->
<script lang="ts">
  import Button from './Button.svelte';

  let { page, pages, onchange }: { page: number; pages: number; onchange: (page: number) => void } =
    $props();
</script>

{#if pages > 1}
  <nav class="ui-pagination" aria-label="Pages">
    <Button variant="secondary" size="sm" disabled={page <= 1} onclick={() => onchange(page - 1)}
      >Previous</Button
    >
    <span class="caption num">{page} / {pages}</span>
    <Button
      variant="secondary"
      size="sm"
      disabled={page >= pages}
      onclick={() => onchange(page + 1)}>Next</Button
    >
  </nav>
{/if}

<style>
  .ui-pagination {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-3);
    padding: var(--space-3) 0;
  }
</style>
