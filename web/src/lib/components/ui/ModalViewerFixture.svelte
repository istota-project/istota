<script lang="ts">
  import { Modal, Button, type ViewerNavigation } from '$lib/components/ui';
  import Lightbox from '$lib/components/Lightbox.svelte';

  interface Props {
    variant?: 'default' | 'viewer';
    bodyKey?: string | number;
    width?: string;
    metadataText?: string;
    navigation?: ViewerNavigation;
  }

  let {
    variant = 'viewer',
    bodyKey = 'first',
    width,
    metadataText = '12 KB',
    navigation,
  }: Props = $props();
  let open = $state(false);
  let imageIndex = $state<number | null>(null);
</script>

<Button onclick={() => (open = true)}>Open viewer</Button>
<Modal
  bind:open
  title="Example document"
  description="Preview"
  {variant}
  {bodyKey}
  {width}
  {navigation}
>
  {#snippet metadata()}<span>{metadataText}</span>{/snippet}
  {#snippet actions()}<Button href="/example.txt" download="example.txt">Download</Button>{/snippet}
  <p>Document body</p>
  <input aria-label="Input" />
  <textarea aria-label="Source" readonly>Source text</textarea>
  <div contenteditable="true" role="textbox" tabindex="0" aria-label="Editable">Edit</div>
  <audio controls aria-label="Audio"></audio>
  <video controls aria-label="Video"><track kind="captions" /></video>
  <Button onclick={() => (imageIndex = 0)}>Zoom image</Button>
  <Lightbox
    images={['/one.jpg', '/two.jpg']}
    index={imageIndex}
    onClose={() => (imageIndex = null)}
  />
  {#snippet footer()}<span>Footer</span>{/snippet}
</Modal>
