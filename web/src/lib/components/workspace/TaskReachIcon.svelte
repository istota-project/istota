<script lang="ts">
  import { Bot } from '@lucide/svelte';
  import type { WorkspaceAccess } from '$lib/api';
  import { taskReachDescription } from './reach';
  let {
    access,
    id,
    room = false,
  }: { access: WorkspaceAccess; id: string; room?: boolean } = $props();
  const description = $derived(taskReachDescription(access, room));
</script>

<!-- Hidden from the tree so the row's link keeps its label as its name; aria-describedby still reads the id. -->
{#if description}
  <span class="task-reach" title={description} aria-hidden="true">
    <Bot size={14} />
    <span {id} class="visually-hidden">{description}</span>
  </span>
{/if}

<style>
  .task-reach {
    display: inline-flex;
    flex-shrink: 0;
    margin-left: auto;
    color: var(--accent-blue);
  }
  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }
</style>
