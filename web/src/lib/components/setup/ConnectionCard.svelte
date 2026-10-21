<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Button, Card } from '@minot/ui';

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
<Card {title} {description} level={3} variant="outline" padding="sm" ariaLabel={title}>
  {#snippet actions()}
    {#if added}
      <Button variant="secondary" size="sm" onclick={onRemove}>Remove</Button>
    {:else}
      <Button variant="secondary" size="sm" onclick={onAdd}>Add</Button>
    {/if}
  {/snippet}
  {#if added}
    <div class="vstack">
      {@render children()}
    </div>
  {/if}
</Card>
