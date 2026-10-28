<script lang="ts">
  import {
    getBriefingArchive,
    getBriefingArchiveItem,
    previewBriefing,
    type BriefingArchiveItem,
    type BriefingBlock,
    type BriefingBlockSave,
    type BriefingDocument,
    type BriefingPreviewResponse,
  } from '$lib/api';
  import { Alert, Button, Modal, OptionCards, PageLoading } from '@minot/ui';
  import BriefingEdition from './BriefingEdition.svelte';

  let {
    briefingName,
    blocks,
    draft,
    onClose,
  }: {
    briefingName: string;
    blocks: BriefingBlock[];
    draft?: BriefingBlockSave;
    onClose: () => void;
  } = $props();
  let mode = $state('web');
  let loading = $state(true);
  let error = $state('');
  let attempt = $state(0);
  let edition = $state<BriefingArchiveItem | null>(null);
  let rendered = $state<BriefingPreviewResponse | null>(null);
  const formats = [
    { value: 'web', title: 'Web' },
    { value: 'html', title: 'HTML email' },
    { value: 'plain', title: 'Plain text' },
  ];
  const proposed = $derived.by(() => {
    const ordered: BriefingBlockSave[] = blocks.map((block) =>
      draft?.id === block.id ? draft : block,
    );
    if (draft && !draft.id) ordered.push(draft);
    return ordered;
  });

  function previewDocument(
    name: string,
    definitions: BriefingBlockSave[],
    saved: BriefingArchiveItem | null,
  ): BriefingDocument {
    // IDs are scoped to this briefing; titles never select example content.
    const source = saved?.briefing_name === name ? saved.document : null;
    const content = new Map(source?.blocks.map((block) => [block.block_id, block]) ?? []);
    return {
      version: 1,
      title:
        (saved?.briefing_name === name && (source?.title || saved.subject)) || `Preview of ${name}`,
      blocks: definitions.map((block) => {
        const previous = block.id === undefined ? undefined : content.get(block.id);
        const width = (block.options?.presentation as { width?: string } | null)?.width;
        return {
          block_id: block.id ?? -1,
          title: block.title?.trim() || 'Untitled block',
          kind: previous?.kind ?? 'markdown',
          body_md: previous?.body_md ?? 'Placeholder: no saved edition content for this block.',
          presentation: { width: width === 'wide' || width === 'full' ? width : 'auto' },
          source_status: previous?.source_status ?? 'complete',
        };
      }),
    };
  }

  function isolatedHtml(html: string): string {
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    for (const anchor of parsed.querySelectorAll('a')) {
      const text = parsed.createElement('span');
      text.textContent = anchor.textContent;
      anchor.replaceWith(text);
    }
    for (const element of parsed.querySelectorAll(
      'script, style, link, meta, base, img, iframe, object, embed, form, audio, video, source',
    ))
      element.remove();
    const policy = parsed.createElement('meta');
    policy.httpEquiv = 'Content-Security-Policy';
    // The mail template's look is inline style attributes; style elements are removed above.
    policy.content =
      "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
    parsed.head.prepend(policy);
    return '<!doctype html>' + parsed.documentElement.outerHTML;
  }
  const html = $derived(rendered ? isolatedHtml(rendered.html) : '');

  $effect(() => {
    const name = briefingName;
    const definitions = proposed;
    void attempt;
    let active = true;
    loading = true;
    error = '';
    edition = null;
    rendered = null;
    async function load() {
      try {
        const latest = await getBriefingArchive({ briefing_name: name, limit: '1' });
        if (!active) return;
        const saved = latest.items.length ? await getBriefingArchiveItem(latest.items[0].id) : null;
        if (!active) return;
        const document = previewDocument(name, definitions, saved);
        edition = {
          id: 0,
          read_at: null,
          briefing_name: name,
          subject: document.title,
          generated_at: saved?.generated_at ?? '',
          task_id: null,
          delivered_to: [],
          document,
        };
        const response = await previewBriefing(document);
        if (active) rendered = response;
      } catch (e) {
        if (active) error = e instanceof Error ? e.message : 'Could not load preview.';
      } finally {
        if (active) loading = false;
      }
    }
    void load();
    return () => {
      active = false;
    };
  });
</script>

<Modal
  open={true}
  variant="viewer"
  title="Briefing preview"
  onOpenChange={(open) => {
    if (!open) onClose();
  }}
>
  <div class="preview">
    <p class="caption">
      This shows saved edition content with the proposed layout. It does not generate new content.
      Blocks without saved content use labelled placeholders.
    </p>
    <OptionCards
      options={formats}
      value={mode}
      label="Preview format"
      onchange={(value) => {
        mode = value;
      }}
    />
    {#if loading}
      <PageLoading label="Loading preview…" />
    {:else}
      {#if error}
        <Alert appearance="inline" variant="danger" role="alert"
          >{error}
          {#snippet actions()}<Button
              variant="secondary"
              onclick={() => {
                attempt += 1;
              }}>Retry preview</Button
            >{/snippet}</Alert
        >
      {/if}
      {#if mode === 'web' && edition}
        <BriefingEdition {edition} showMetadata={false} />
      {:else if mode === 'html' && rendered}
        <iframe title="HTML email preview" sandbox="" srcdoc={html} referrerpolicy="no-referrer"
        ></iframe>
      {:else if mode === 'plain' && rendered}
        <pre aria-label="Plain text preview">{rendered.plain}</pre>
      {/if}
    {/if}
  </div>
</Modal>

<style>
  .preview {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    min-width: 0;
  }
  .preview :global(.briefing-block) {
    background: var(--surface-base);
  }
  p {
    margin: 0;
  }
  iframe {
    width: 100%;
    min-height: 32rem;
    border: 1px solid var(--border-subtle);
    color-scheme: light;
  }
  pre {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font: inherit;
    user-select: text;
  }
</style>
