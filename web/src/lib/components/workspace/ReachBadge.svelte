<script lang="ts">
  import { Bot } from '@lucide/svelte';
  import { Badge } from '$lib/components/ui';
  import type { WorkspaceAccess } from '$lib/api';
  import { accessLabel, reachesTasks } from './reach';
  let { access }: { access: WorkspaceAccess } = $props();
</script>

<Badge size="sm"
  ><span class="reach-label"
    >{#if reachesTasks(access)}<Bot size={12} aria-hidden="true" />{/if}{accessLabel(access)}</span
  ></Badge
>

<style>
  .reach-label {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }
</style>
