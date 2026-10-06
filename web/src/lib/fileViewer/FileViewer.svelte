<script lang="ts">
  import { fileLinks } from '$lib/fileViewer/links';
  import hljs from 'highlight.js/lib/common';
  import { Modal, Button, TextArea } from '$lib/components/ui';
  import { chatFileUrl, previewChatFile, type FilePreview } from '$lib/api';
  import { formatBytes } from '$lib/format';
  import { formatDateTime } from '$lib/dateFormat';
  import { renderDocument } from '$lib/markdown';
  import { presentationFor, splitFrontmatter, HIGHLIGHT_MAX_CHARS } from './presentation';
  import { viewer } from './store.svelte';
  import PdfPreview from './PdfPreview.svelte';

  let { path, onClose }: { path: string; onClose: () => void } = $props();
  let preview = $state<FilePreview | null>(null);
  let error = $state('');
  let canDownload = $state(true);
  let source = $state(false);
  let mediaError = $state(false);
  let media = $state<HTMLMediaElement | undefined>();
  let name = $derived(preview?.name ?? path.split('/').pop() ?? 'File');
  let text = $derived(preview?.text ?? '');
  let presentation = $derived(presentationFor(name));
  let split = $derived(splitFrontmatter(text));
  let language = $derived(presentation.kind === 'code' ? presentation.language : null);
  let highlighted = $derived(
    language && text.length <= HIGHLIGHT_MAX_CHARS
      ? hljs.highlight(text, { language, ignoreIllegals: true }).value
      : null,
  );

  $effect(() => {
    const requested = path;
    const controller = new AbortController();
    let active = true;
    preview = null;
    error = '';
    source = false;
    mediaError = false;
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
  $effect(() => {
    const player = media;
    return () => {
      if (player) {
        player.pause();
        player.removeAttribute('src');
        player.load();
      }
    };
  });
</script>

<Modal
  open
  title={name}
  description={preview
    ? `${formatBytes(preview.size)} · modified ${formatDateTime(preview.modified, { dateStyle: 'medium', timeStyle: 'short' })}`
    : undefined}
  width="min(960px, 100vw)"
  height="100dvh"
  onOpenChange={(open) => {
    if (!open) onClose();
  }}
>
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
      {#if mediaError}
        <p role="alert">
          Your browser could not play this file. Download it to open in another app.
        </p>
      {:else if preview.kind === 'audio'}
        <audio
          bind:this={media}
          src={chatFileUrl(path)}
          controls
          preload="metadata"
          onerror={() => (mediaError = true)}
        ></audio>
      {:else}
        <!-- svelte-ignore a11y_media_has_caption -->
        <video
          bind:this={media}
          src={chatFileUrl(path)}
          controls
          preload="metadata"
          playsinline
          onerror={() => (mediaError = true)}
        ></video>
      {/if}
    {:else if preview.kind === 'text'}
      {#if presentation.kind === 'markdown'}
        <div class="view-controls" role="group" aria-label="Document view">
          <Button variant={source ? 'ghost' : 'secondary'} onclick={() => (source = false)}
            >Rendered</Button
          >
          <Button variant={source ? 'secondary' : 'ghost'} onclick={() => (source = true)}
            >Source</Button
          >
        </div>
      {/if}
      <div use:fileLinks class="file-body">
        {#if presentation.kind === 'markdown' && !source}
          <div class="markdown prose">
            {#if split.frontmatter !== null}<details>
                <summary>Frontmatter</summary>
                <pre>{split.frontmatter}</pre>
              </details>{/if}
            {@html renderDocument(split.body)}
          </div>
        {:else if highlighted !== null}
          <div class="markdown"><pre><code class="hljs">{@html highlighted}</code></pre></div>
        {:else}
          <TextArea
            value={text}
            rows={18}
            monospace
            readonly
            spellcheck="false"
            aria-label={`Source of ${name}`}
          />
        {/if}
      </div>
    {/if}
  {/if}
  {#snippet footer()}
    {#if canDownload}<Button href={chatFileUrl(path)} download={name}>Download</Button>{/if}
    <Button onclick={onClose}>Close</Button>
  {/snippet}
</Modal>

<style>
  .view-controls {
    display: flex;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }
  .file-body {
    font-size: var(--text-base);
    overflow-wrap: anywhere;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: var(--font-mono);
  }
  details pre,
  pre code {
    font-size: var(--text-sm);
  }
  audio,
  video {
    display: block;
    width: 100%;
  }
  video {
    max-height: 65dvh;
  }
</style>
