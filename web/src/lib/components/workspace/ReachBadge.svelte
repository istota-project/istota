<script lang="ts">
  import { Bot } from '@lucide/svelte';
  import { Icon, Badge } from '$lib/components/ui';
  import type { WorkspaceAccess } from '$lib/api';
  import { accessLabel, accessVariant, reachesTasks } from './reach';
  let { access }: { access: WorkspaceAccess } = $props();
</script>

{#snippet bot()}<Icon icon={Bot} size="xs" />{/snippet}

<Badge size="sm" variant={accessVariant(access)} icon={reachesTasks(access) ? bot : undefined}
  >{accessLabel(access)}</Badge
>
