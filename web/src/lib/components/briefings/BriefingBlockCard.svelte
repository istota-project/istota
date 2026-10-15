<script lang="ts">
  import type { BriefingDocumentBlock } from '$lib/api';
  import { Maximize2, Settings2 } from '@lucide/svelte';
  import { HintPopover, IconButton } from '$lib/components/ui';
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
  <section class="briefing-block" data-width={block.presentation.width} aria-label={block.title}>
    <div data-masonry-content>
      <header class="block-head">
        <div class="block-title">
          <h2>{block.title}</h2>
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
              class="briefing-action touch-target"
              size="sm"
              label={`Settings for ${block.title}`}
              title="Block settings"
              onclick={onEdit}><Settings2 size={14} /></IconButton
            >
          {/if}
          <IconButton
            class="briefing-action touch-target"
            size="sm"
            label={`Expand ${block.title}`}
            title="Expand block"
            onclick={() => onReadFull?.(block)}><Maximize2 size={14} /></IconButton
          >
        </div>
      </header>
      {@render body()}
    </div>
  </section>
{/if}

<style>
  .briefing-block {
    box-sizing: border-box;
    min-width: 0;
    padding: var(--space-4);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-card);
    background: var(--surface-card);
  }

  .briefing-block[data-width='full'] {
    grid-column: 1 / -1;
  }

  @container briefing-edition (min-width: 40rem) {
    .briefing-block[data-width='wide'] {
      grid-column: span 2;
    }
  }

  [data-masonry-content] {
    display: flow-root;
  }

  .block-head {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    padding-bottom: var(--space-3);
    margin-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
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
  }

  h2 {
    margin: 0;
    font-size: var(--text-base);
    font-weight: 600;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  .block-body {
    font-size: var(--text-base);
    line-height: 1.5;
    color: var(--text-reading);
    overflow-wrap: anywhere;
    min-width: 0;
  }
</style>
