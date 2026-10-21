<script lang="ts">
  import { Modal, Button, type ViewerNavigation } from '@minot/ui';
  import Lightbox from '$lib/components/Lightbox.svelte';

  interface Props {
    navigation?: ViewerNavigation;
  }

  let { navigation }: Props = $props();
  let open = $state(false);
  let imageIndex = $state<number | null>(null);
</script>

<Button onclick={() => (open = true)}>Open viewer</Button>
<Modal bind:open title="Example document" variant="viewer" {navigation}>
  <p>Document body</p>
  <Button onclick={() => (imageIndex = 0)}>Zoom image</Button>
  <Lightbox
    images={['/one.jpg', '/two.jpg']}
    index={imageIndex}
    onClose={() => (imageIndex = null)}
  />
</Modal>
