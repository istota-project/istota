<script lang="ts">
  import { Badge } from '$lib/components/ui';
  import type { WorkspaceAccess } from '$lib/api';
  import { ACCESS_LABELS } from './reach';
  let { access }: { access: WorkspaceAccess } = $props();
</script>

<Badge size="sm">{ACCESS_LABELS[access] ?? access}</Badge>
