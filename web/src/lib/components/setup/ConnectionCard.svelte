<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Button } from '$lib/components/ui';

  interface Props {
    title: string;
    description: string;
    /** Added: the fields show, and Remove takes them away again. */
    added: boolean;
    onAdd: () => void;
    onRemove: () => void;
    children: Snippet;
  }

  let { title, description, added, onAdd, onRemove, children }: Props = $props();
</script>

<!--
  One optional connection on the Email and calendar screen: a title and a line
  until it is added, then its fields and its Test.
-->
<section class="connection" aria-label={title}>
  <div class="connection-head">
    <div>
      <h3>{title}</h3>
      <p class="caption">{description}</p>
    </div>
    {#if added}
      <Button variant="ghost" size="sm" onclick={onRemove}>Remove</Button>
    {:else}
      <Button variant="secondary" size="sm" onclick={onAdd}>Add</Button>
    {/if}
  </div>
  {#if added}
    <div class="connection-body">
      {@render children()}
    </div>
  {/if}
</section>

<style>
  .connection {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-3);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
  }

  .connection-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-3);
  }

  h3 {
    margin: 0;
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  p {
    margin: 0;
  }

  .connection-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
</style>
