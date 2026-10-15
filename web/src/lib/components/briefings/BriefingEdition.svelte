<script lang="ts">
  import type { BriefingArchiveItem, BriefingDocument, BriefingDocumentBlock } from '$lib/api';
  import { HintPopover, Modal } from '$lib/components/ui';
  import { formatDateTime } from '$lib/dateFormat';
  import { fileLinks } from '$lib/fileViewer/links';
  import { masonry } from '$lib/masonry';
  import { renderMarkdown } from '$lib/markdown';
  import BriefingBlockCard from './BriefingBlockCard.svelte';

  let {
    edition,
    showMetadata = true,
    editableBlockIds = [],
    onEditBlock,
  }: {
    edition: BriefingArchiveItem;
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

<article class="content-frame edition">
  <header class="edition-head">
    <h1>{edition.subject || edition.briefing_name}</h1>
    {#if showMetadata}
      <p class="caption">
        {formatDateTime(edition.generated_at, { dateStyle: 'medium', timeStyle: 'short' })}
        {#if edition.delivered_to?.length}
          <span> · delivered to {edition.delivered_to.join(', ')}</span>
        {/if}
      </p>
    {/if}
  </header>
  {#if document}
    <div class="edition-grid" use:masonry>
      {#each document.blocks as block}
        <BriefingBlockCard
          {block}
          onReadFull={readFull}
          onEdit={onEditBlock && editableBlockIds.includes(block.block_id)
            ? () => onEditBlock?.(block.block_id)
            : undefined}
        />
      {/each}
    </div>
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
    container: briefing-edition / inline-size;
    min-width: 0;
  }

  .edition-head {
    padding-bottom: var(--space-4);
    margin-bottom: var(--space-4);
    border-bottom: 1px solid var(--border-subtle);
    overflow-wrap: anywhere;
  }

  .edition-head h1 {
    margin: 0 0 var(--space-2);
    font-size: var(--text-xl);
  }

  .caption {
    margin: 0;
  }

  .edition-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-items: start;
    gap: var(--space-4);
  }

  .edition-grid:global([data-masonry='true']) {
    grid-auto-rows: 1px;
    row-gap: 0;
  }

  @container briefing-edition (min-width: 40rem) {
    .edition-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @container briefing-edition (min-width: 64rem) {
    .edition-grid {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
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
