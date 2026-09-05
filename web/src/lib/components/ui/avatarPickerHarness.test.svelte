<script lang="ts">
  /* AvatarPicker takes its picture as a snippet, which a test cannot pass
     through `render`'s props. The harness supplies a stand-in and forwards
     everything else. */
  import AvatarPicker from './AvatarPicker.svelte';

  let {
    pickLabel = 'Choose a picture',
    prompt = 'Click the picture to choose a file, or drop or paste one here.',
    accept = 'image/png',
    busyLabel = '',
    removable = false,
    onPick,
    onRemove,
  }: {
    pickLabel?: string;
    prompt?: string;
    accept?: string;
    busyLabel?: string;
    removable?: boolean;
    onPick: (file: File) => void;
    onRemove?: () => void;
  } = $props();
</script>

<AvatarPicker {pickLabel} {prompt} {accept} {busyLabel} {removable} {onPick} {onRemove}>
  {#snippet preview()}
    <span data-testid="preview">picture</span>
  {/snippet}
</AvatarPicker>
