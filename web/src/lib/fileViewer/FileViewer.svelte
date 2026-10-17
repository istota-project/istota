<script lang="ts">
  import { Modal, Button, IconButton } from '$lib/components/ui';
  import { Code, Download, ExternalLink, Eye } from '@lucide/svelte';
  import MediaPreview from '$lib/components/viewer/MediaPreview.svelte';
  import TextPreview from '$lib/components/viewer/TextPreview.svelte';
  import { base } from '$app/paths';
  import {
    chatFileUrl,
    nextcloudFileUrl,
    previewChatFile,
    previewNextcloudFile,
    type FilePreview,
  } from '$lib/api';
  import { formatBytes } from '$lib/format';
  import { formatDateTime } from '$lib/dateFormat';
  import { viewer, type FileSource } from './store.svelte';
  import PdfPreview from './PdfPreview.svelte';
  import { presentationFor } from './presentation';

  let {
    path,
    fileSource = { kind: 'workspace' },
    onClose,
  }: { path: string; fileSource?: FileSource; onClose: () => void } = $props();
  let nextcloud = $derived(fileSource.kind === 'nextcloud');
  let fileUrl = $derived(nextcloud ? nextcloudFileUrl : chatFileUrl);
  let webUrl = $derived(fileSource.kind === 'nextcloud' ? fileSource.webUrl : null);
  // A `url` source names a PDF endpoint directly: no preview call, no sniffing.
  let direct = $derived(fileSource.kind === 'url' ? fileSource : null);
  let missing = $state(false);
  let reconnect = $state(false);
  let preview = $state<FilePreview | null>(null);
  let error = $state('');
  let canDownload = $state(true);
  let source = $state(false);
  let name = $derived(direct?.name ?? preview?.name ?? path.split('/').pop() ?? 'File');
  let markdown = $derived(preview?.kind === 'text' && presentationFor(name).kind === 'markdown');
  // The year only when it is not this one (at every width), so a phone keeps size and time on one line.
  function shortModified(iso: string): string {
    const sameYear = new Date(iso).getFullYear() === new Date().getFullYear();
    return formatDateTime(iso, {
      month: 'short',
      day: 'numeric',
      year: sameYear ? undefined : 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  }
  $effect(() => {
    const requested = path;
    const controller = new AbortController();
    let active = true;
    preview = null;
    error = '';
    source = false;
    canDownload = true;
    reconnect = false;
    missing = false;
    if (direct) return;
    const load = nextcloud ? previewNextcloudFile : previewChatFile;
    const urlFor = fileUrl;
    load(requested, controller.signal)
      .then((result) => {
        if (!active) return;
        if (result.kind === 'image') viewer.openImages([urlFor(requested)], 0);
        else preview = result;
      })
      .catch((e: unknown) => {
        if (!active) return;
        const status = e && typeof e === 'object' && 'status' in e ? Number(e.status) : null;
        canDownload = ![400, 401, 403, 404, 409].includes(status ?? 0);
        // The personal connection is missing or revoked; only Settings can fix it.
        reconnect = nextcloud && status === 409;
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
  bodyPadding="0"
  onOpenChange={(open) => {
    if (!open) onClose();
  }}
>
  {#snippet metadata()}
    {#if preview}
      {formatBytes(preview.size)} · <span class="metadata-verb">modified</span>{' '}<time
        datetime={preview.modified}
        title={formatDateTime(preview.modified, { dateStyle: 'medium', timeStyle: 'short' })}
        >{shortModified(preview.modified)}</time
      >
    {/if}
  {/snippet}
  {#snippet actions()}
    {#if markdown}<div class="view-toggle" role="group" aria-label="Document view">
        <IconButton
          label="Rendered"
          title="Rendered"
          active={!source}
          pressed={!source}
          onclick={() => (source = false)}><Eye size={18} /></IconButton
        >
        <IconButton
          label="Source"
          title="Source"
          active={source}
          pressed={source}
          onclick={() => (source = true)}><Code size={18} /></IconButton
        >
      </div>{/if}
    {#if reconnect}<Button size="sm" variant="secondary" href={`${base}/settings/connections`}
        >Reconnect</Button
      >{/if}
    {#if webUrl}<IconButton
        label="Open in Nextcloud"
        title="Open in Nextcloud"
        href={webUrl}
        target="_blank"
        rel="noopener noreferrer"><ExternalLink size={18} /></IconButton
      >{/if}
    {#if direct}
      {#if !missing}<IconButton label="Download" title="Download" href={direct.url} download=""
          ><Download size={18} /></IconButton
        >{/if}
    {:else if canDownload}<IconButton
        label="Download"
        title="Download"
        href={fileUrl(path)}
        download={name}><Download size={18} /></IconButton
      >{/if}
  {/snippet}
  <div class="file-content">
    {#if direct}
      {#if missing}
        <p role="alert">This file is no longer available.</p>
      {:else}
        <PdfPreview
          url={direct.url}
          onLoadError={(status) => {
            if (status === 404) missing = true;
          }}
        />
      {/if}
    {:else if error}
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
        <PdfPreview url={fileUrl(path)} />
      {:else if preview.kind === 'audio' || preview.kind === 'video'}
        <div class="file-media">
          <MediaPreview
            kind={preview.kind}
            url={fileUrl(path)}
            failureMessage="Your browser could not play this file. Download it to open in another app."
          />
        </div>
      {:else if preview.kind === 'text'}
        <TextPreview {name} text={preview.text ?? ''} {source} fill untrusted={nextcloud} />
      {/if}
    {/if}
  </div>
</Modal>

<style>
  .view-toggle {
    display: flex;
    gap: var(--space-1);
    padding: 2px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
  }
  /* The selected half is filled, so hovering the other half can't look like a selection. */
  .view-toggle :global(.icon-btn.active),
  .view-toggle :global(.icon-btn.active:hover) {
    background: var(--accent);
    color: var(--surface-base);
  }
  @media (max-width: 640px) {
    .metadata-verb {
      display: none;
    }
  }
  .file-content {
    height: 100%;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .file-content > p {
    flex-shrink: 0;
    margin: 0;
    padding: var(--space-2) var(--space-3);
  }
  .file-media {
    padding: var(--space-2) var(--space-3);
  }
</style>
