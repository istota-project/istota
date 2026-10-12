<script lang="ts">
  import { Badge } from '$lib/components/ui';
  import type { WorkspaceAccess } from '$lib/api';
  let { access }: { access: WorkspaceAccess } = $props();
  const labels = { rw: 'Read-write', ro: 'Read-only', skill: 'Via skill', none: 'No access' };
</script>

<Badge size="sm">{labels[access]}</Badge>
