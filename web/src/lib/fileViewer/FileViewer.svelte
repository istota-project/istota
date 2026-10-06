<script lang="ts">
  import { Modal, Button } from '$lib/components/ui';
  import MediaPreview from '$lib/components/viewer/MediaPreview.svelte';
  import TextPreview from '$lib/components/viewer/TextPreview.svelte';
  import { chatFileUrl, previewChatFile, type FilePreview } from '$lib/api';
  import { formatBytes } from '$lib/format';
  import { formatDateTime } from '$lib/dateFormat';
  import { presentationFor } from './presentation';
  import { viewer } from './store.svelte';
  import PdfPreview from './PdfPreview.svelte';

  let { path, onClose }: { path: string; onClose: () => void } = $props();
  let preview = $state<FilePreview | null>(null);
  let error = $state('');
  let canDownload = $state(true);
  let source = $state(false);
  let name = $derived(preview?.name ?? path.split('/').pop() ?? 'File');
  let wide = $derived(
    preview?.kind === 'pdf' ||
      (preview?.kind === 'text' && (source || presentationFor(name).kind === 'code')),
  );
  $effect(() => {
    const requested = path;
    const controller = new AbortController();
    let active = true;
    preview = null;
    error = '';
    source = false;
    canDownload = true;
    previewChatFile(requested, controller.signal)
      .then((result) => {
        if (!active) return;
        if (result.kind === 'image') viewer.openImages([chatFileUrl(requested)], 0);
        else preview = result;
      })
      .catch((e: unknown) => {
        if (!active) return;
        const status = e && typeof e === 'object' && 'status' in e ? Number(e.status) : null;
        canDownload = ![400, 401, 403, 404].includes(status ?? 0);
        error = status && e instanceof Error ? e.message : "Couldn't load a preview.";
      });
    return () => {
      active = false;
      controller.abort();
    };
  });
</script>

<Modal
  open
  variant="viewer"
  title={name}
  bodyKey={path}
  width={wide ? '960px' : '720px'}
  onOpenChange={(open) => {
    if (!open) onClose();
  }}
>
  {#snippet metadata()}
    {#if preview}
      {formatBytes(preview.size)} · modified
      <time datetime={preview.modified}
        >{formatDateTime(preview.modified, { dateStyle: 'medium', timeStyle: 'short' })}</time
      >
    {/if}
  {/snippet}
  {#snippet actions()}
    {#if canDownload}<Button size="sm" href={chatFileUrl(path)} download={name}>Download</Button
      >{/if}
  {/snippet}
  {#if error}
    <p role="alert">{error}</p>
  {:else if !preview}
    <p class="muted">Loading…</p>
  {:else}
    {#if preview.truncated}<p class="muted">
        Showing the first 1 MiB. Download for the full file.
      </p>{/if}
    {#if preview.kind === 'binary'}
      <p>No preview for this file type.</p>
    {:else if preview.kind === 'pdf'}
      <PdfPreview url={chatFileUrl(path)} />
    {:else if preview.kind === 'audio' || preview.kind === 'video'}
      <MediaPreview
        kind={preview.kind}
        url={chatFileUrl(path)}
        failureMessage="Your browser could not play this file. Download it to open in another app."
      />
    {:else if preview.kind === 'text'}
      <TextPreview {name} text={preview.text ?? ''} bind:source />
    {/if}
  {/if}
</Modal>
