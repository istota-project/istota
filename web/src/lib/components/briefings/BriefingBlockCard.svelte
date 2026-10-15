<script lang="ts">
  import type { BriefingDocumentBlock } from '$lib/api';
  import { Button } from '$lib/components/ui';
  import { fileLinks } from '$lib/fileViewer/links';
  import { renderMarkdown, renderMarkdownPreview } from '$lib/markdown';

  interface Props {
    block: BriefingDocumentBlock;
    fullContent?: boolean;
    onReadFull?: (block: BriefingDocumentBlock) => void;
  }

  let { block, fullContent = false, onReadFull }: Props = $props();
  const content = $derived(
    fullContent
      ? { html: renderMarkdown(block.body_md), truncated: false }
      : renderMarkdownPreview(block.body_md),
  );
</script>

{#snippet body()}
  <div use:fileLinks class="block-body markdown prose">{@html content.html}</div>
{/snippet}

{#if fullContent}
  {@render body()}
{:else}
  <section class="briefing-block" data-width={block.presentation.width} aria-label={block.title}>
    <header class="card-head">
      <h2>{block.title}</h2>
      {#if block.source_status === 'partial'}
        <span class="caption">Some sources unavailable</span>
      {/if}
    </header>
    {@render body()}
    {#if content.truncated}
      <div class="read-full">
        <Button size="sm" onclick={() => onReadFull?.(block)}>Read full block</Button>
      </div>
    {/if}
  </section>
{/if}

<style>
  .briefing-block {
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

  .card-head {
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-bottom: var(--space-4);
  }

  h2 {
    margin: 0;
    font-size: var(--text-lg);
    overflow-wrap: anywhere;
  }

  .block-body {
    font-size: var(--text-base);
    line-height: 1.5;
    color: var(--text-reading);
    overflow-wrap: anywhere;
    min-width: 0;
  }

  .read-full {
    margin-top: var(--space-4);
  }
</style>
