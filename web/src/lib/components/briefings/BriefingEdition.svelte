<script lang="ts">
  import type { BriefingArchiveItem, BriefingDocument, BriefingDocumentBlock } from '$lib/api';
  import { CardGrid, HintPopover, Modal } from '$lib/components/ui';
  import { formatDateTime } from '$lib/dateFormat';
  import { fileLinks } from '$lib/fileViewer/links';
  import { renderMarkdown } from '$lib/markdown';
  import BriefingBlockCard from './BriefingBlockCard.svelte';
  import { channelLabel } from './settings/briefingSettings';
  import type { Snippet } from 'svelte';

  let {
    edition,
    showMetadata = true,
    editableBlockIds = [],
    onEditBlock,
    meta,
    actions,
  }: {
    edition: BriefingArchiveItem;
    meta?: Snippet;
    actions?: Snippet;
    showMetadata?: boolean;
    editableBlockIds?: number[];
    onEditBlock?: (blockId: number) => void;
  } = $props();
  let fullBlock = $state<BriefingDocumentBlock | null>(null);
  let viewerOpen = $state(false);
  const document = $derived(validDocument(edition.document) ? edition.document : null);

  function validDocument(value: BriefingDocument | null | undefined): value is BriefingDocument {
    if (
      value?.version !== 1 ||
      typeof value.title !== 'string' ||
      !value.title.trim() ||
      !Array.isArray(value.blocks)
    )
      return false;
    const ids = new Set<number>();
    for (const block of value.blocks) {
      if (
        !block ||
        !Number.isSafeInteger(block.block_id) ||
        ids.has(block.block_id) ||
        typeof block.title !== 'string' ||
        !block.title.trim() ||
        typeof block.kind !== 'string' ||
        !block.kind.trim() ||
        typeof block.body_md !== 'string' ||
        !['auto', 'wide', 'full'].includes(block.presentation?.width) ||
        !['complete', 'partial'].includes(block.source_status)
      )
        return false;
      ids.add(block.block_id);
    }
    return true;
  }

  function readFull(block: BriefingDocumentBlock) {
    fullBlock = block;
    viewerOpen = true;
  }
</script>

<article class="edition">
  <header class="edition-head">
    <div class="edition-title">
      <h1>{edition.subject || edition.briefing_name}</h1>
      {#if actions}{@render actions()}{/if}
    </div>
    {#if showMetadata}
      <p class="caption edition-meta">
        <span>Just me</span>
        <time datetime={edition.generated_at}
          >{formatDateTime(edition.generated_at, { dateStyle: 'medium', timeStyle: 'short' })}</time
        >
        {#if edition.delivered_to?.length}
          <span>Delivered by {edition.delivered_to.map(channelLabel).join(', ')}</span>
        {/if}
      </p>
    {/if}
    {#if meta}{@render meta()}{/if}
  </header>
  {#if document}
    <CardGrid masonry>
      {#each document.blocks as block}
        <BriefingBlockCard
          {block}
          onReadFull={readFull}
          onEdit={onEditBlock && editableBlockIds.includes(block.block_id)
            ? () => onEditBlock?.(block.block_id)
            : undefined}
        />
      {/each}
    </CardGrid>
  {:else}
    <div class="legacy markdown prose" use:fileLinks>
      {@html renderMarkdown(edition.body_md ?? '')}
    </div>
  {/if}
</article>

{#if fullBlock}
  <Modal
    bind:open={viewerOpen}
    variant="viewer"
    title={fullBlock.title}
    bodyKey={fullBlock.block_id}
  >
    {#snippet metadata()}
      {#if fullBlock?.source_status === 'partial'}
        <HintPopover
          variant="warning"
          label="Some sources unavailable"
          text="Some sources unavailable"
        />
      {/if}
    {/snippet}
    <BriefingBlockCard block={fullBlock} fullContent />
  </Modal>
{/if}

<style>
  .edition {
    min-width: 0;
  }

  .edition-head {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin-bottom: var(--space-6);
    overflow-wrap: anywhere;
  }

  /* One line of facts, dot-separated, shared with the group edition's meta. */
  .edition-head :global(.edition-meta) {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1) var(--space-2);
    margin: 0;
  }

  .edition-head :global(.edition-meta > * + *::before) {
    content: '·';
    margin-right: var(--space-2);
  }

  .edition-title {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
  }

  .edition-head h1 {
    flex: 1;
    min-width: 0;
    margin: 0;
    font-size: var(--text-xl);
    line-height: 1.3;
  }

  .legacy {
    min-width: 0;
    padding: var(--space-4);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-card);
    background: var(--surface-card);
    color: var(--text-reading);
    font-size: var(--text-base);
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
</style>
