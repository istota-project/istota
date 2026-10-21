<script lang="ts">
  import type { BriefingDocumentBlock } from '$lib/api';
  import { Maximize2, Settings2 } from '@lucide/svelte';
  import { Card, HintPopover, IconButton } from '@minot/ui';
  import { fileLinks } from '$lib/fileViewer/links';
  import { renderMarkdown } from '$lib/markdown';

  interface Props {
    block: BriefingDocumentBlock;
    fullContent?: boolean;
    onEdit?: () => void;
    onReadFull?: (block: BriefingDocumentBlock) => void;
  }

  let { block, fullContent = false, onReadFull, onEdit }: Props = $props();
  const content = $derived(renderMarkdown(block.body_md));
</script>

{#snippet body()}
  <div use:fileLinks class="block-body markdown prose">{@html content}</div>
{/snippet}

{#if fullContent}
  {@render body()}
{:else}
  <Card class="briefing-block" data-width={block.presentation.width} ariaLabel={block.title}>
    <div data-masonry-content>
      <header class="block-head">
        <div class="block-title">
          <h2 class="micro-label">{block.title}</h2>
          {#if block.source_status === 'partial'}
            <HintPopover
              variant="warning"
              label="Some sources unavailable"
              text="Some sources unavailable"
            />
          {/if}
        </div>
        <div class="block-actions">
          {#if onEdit}
            <IconButton
              class="briefing-action"
              touchTarget
              size="sm"
              label={`Settings for ${block.title}`}
              title="Block settings"
              onclick={onEdit}
              icon={Settings2}
            />
          {/if}
          <IconButton
            class="briefing-action"
            touchTarget
            size="sm"
            label={`Expand ${block.title}`}
            title="Expand block"
            onclick={() => onReadFull?.(block)}
            icon={Maximize2}
          />
        </div>
      </header>
      {@render body()}
    </div>
  </Card>
{/if}

<style>
  [data-masonry-content] {
    display: flow-root;
  }

  .block-head {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    margin-bottom: var(--space-2);
  }

  .block-title {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    flex: 1;
    min-width: 0;
  }
  .block-actions {
    display: flex;
    flex-shrink: 0;
    gap: var(--space-1);
  }

  .block-actions :global(.briefing-action) {
    width: 1.25rem;
    height: 1.25rem;
    min-height: 0;
    padding: 0;
    border-radius: var(--radius-sm);
    color: var(--text-dim);
  }

  /* A section label rather than a heading the size of the content's own, so
     the block's name and the stories inside it never read as one level. */
  h2 {
    margin: 0;
    line-height: 1.25rem;
    overflow-wrap: anywhere;
  }

  .block-body {
    font-size: var(--text-base);
    line-height: var(--leading-normal);
    color: var(--text-reading);
    overflow-wrap: anywhere;
    min-width: 0;
  }
</style>
