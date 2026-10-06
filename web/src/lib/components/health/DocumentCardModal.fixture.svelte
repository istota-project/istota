<script lang="ts">
  import type { HealthDocument } from '$lib/api';
  import { Modal, Button } from '$lib/components/ui';
  import DocumentCard from './DocumentCard.svelte';
  import FileViewerHost from '$lib/fileViewer/FileViewerHost.svelte';
  let { doc }: { doc: HealthDocument } = $props();
  let open = $state(true);
</script>

<Modal bind:open title="Documents">
  <DocumentCard {doc} />
  <Button onclick={() => (open = false)}>Done</Button>
</Modal>
<FileViewerHost />
