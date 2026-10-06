<script lang="ts">
  import Lightbox from '$lib/components/Lightbox.svelte';
  import FileViewer from './FileViewer.svelte';
  import { viewer } from './store.svelte';
</script>

{#if viewer.state.mode === 'file'}
  {#key viewer.state}
    <FileViewer path={viewer.state.path} onClose={viewer.close} />
  {/key}
{/if}
<Lightbox
  images={viewer.state.mode === 'images' ? viewer.state.images : []}
  index={viewer.state.mode === 'images' ? viewer.state.index : null}
  download
  onClose={viewer.close}
/>
