<script lang="ts">
  import { Bot } from '@lucide/svelte';
  import { Badge } from '$lib/components/ui';
  import type { WorkspaceAccess } from '$lib/api';
  import { accessLabel, accessVariant, reachesTasks } from './reach';
  let { access }: { access: WorkspaceAccess } = $props();
</script>

{#snippet bot()}<Bot size={12} aria-hidden="true" />{/snippet}

<Badge size="sm" variant={accessVariant(access)} icon={reachesTasks(access) ? bot : undefined}
  >{accessLabel(access)}</Badge
>
