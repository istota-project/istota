<script lang="ts">
  import type { BriefingBlock, BriefingBlockSave, BriefingBlockTemplate } from '$lib/api';
  import { deleteBriefingBlock, putBriefingBlock } from '$lib/api';
  import {
    Button,
    ConfirmDialog,
    EmptyState,
    Input,
    KebabMenu,
    Modal,
    type KebabItem,
    type SelectOption,
  } from '$lib/components/ui';
  import BlockEditor from './BlockEditor.svelte';
  import BriefingPreview from './BriefingPreview.svelte';

  interface Props {
    briefingName: string;
    editBlockId?: number | null;
    blocks: BriefingBlock[];
    templates: BriefingBlockTemplate[];
    sourceKinds: string[];
    browseOptions: SelectOption[];
    rssOptions: SelectOption[];
    sharedBlockOptions: SelectOption[];
    feedsAvailable: boolean;
    onChange: (briefingName: string, blocks: BriefingBlock[]) => void;
  }
  let {
    briefingName,
    editBlockId = null,
    blocks,
    templates,
    sourceKinds,
    browseOptions,
    rssOptions,
    sharedBlockOptions,
    feedsAvailable,
    onChange,
  }: Props = $props();
  let picking = $state(false);
  let previewing = $state(false);
  let search = $state('');
  let draft = $state<BriefingBlockSave | null>(null);
  let deleting = $state<BriefingBlock | null>(null);
  let busy = $state(false);
  let error = $state('');
  let deleteError = $state('');
  let announcement = $state('');
  const filtered = $derived(
    templates.filter((template) =>
      `${template.label} ${template.description}`
        .toLowerCase()
        .includes(search.trim().toLowerCase()),
    ),
  );

  function blockPayload(block: BriefingBlock, copy = false): BriefingBlockSave {
    return JSON.parse(
      JSON.stringify({
        ...(!copy ? { id: block.id } : {}),
        briefing_name: briefingName,
        title: `${block.title}${copy ? ' (copy)' : ''}`,
        directive: block.directive ?? '',
        render_mode: block.render_mode,
        options: block.options ?? {},
        sources: block.sources.map((source) => ({
          ...(!copy ? { id: source.id } : {}),
          kind: source.kind,
          config: source.config ?? {},
          enabled: source.enabled,
        })),
      }),
    );
  }

  let openedEditorId = $state<number | null>(null);
  $effect(() => {
    if (!editBlockId || openedEditorId === editBlockId) return;
    const block = blocks.find((item) => item.id === editBlockId);
    if (block) {
      openedEditorId = editBlockId;
      draft = blockPayload(block);
    }
  });

  function unavailable(template: BriefingBlockTemplate): string {
    if (template.block.sources.some((source) => source.kind === 'rss') && !feedsAvailable)
      return 'Enable Feeds and subscribe to a feed first.';
    if (
      template.block.sources.some((source) => source.kind === 'shared_block') &&
      !sharedBlockOptions.length
    )
      return 'No shared blocks are available yet.';
    return '';
  }

  function choose(template: BriefingBlockTemplate) {
    if (unavailable(template)) return;
    draft = JSON.parse(
      JSON.stringify({
        ...template.block,
        briefing_name: briefingName,
        sources: template.block.sources.map((source) => ({
          ...source,
          config: source.config ?? {},
          enabled: source.enabled !== false,
        })),
      }),
    );
    picking = false;
  }

  function widthLabel(block: BriefingBlock): string {
    const width = (block.options?.presentation as { width?: string } | null)?.width;
    return width === 'wide' ? 'Wide' : width === 'full' ? 'Full width' : 'Automatic';
  }

  function sourceSummary(block: BriefingBlock): string {
    if (!block.sources.length) return 'No sources';
    return block.sources
      .map((source) => {
        const config = source.config;
        let label = source.kind;
        if (config.path) label += `: ${config.path}`;
        else if (config.url) label += `: ${config.url}`;
        else if (config.preset) label += `: ${config.preset}`;
        else if (config.name) label += `: ${config.name}`;
        return `${label}${source.enabled ? '' : ' (disabled)'}`;
      })
      .join(' · ');
  }

  async function save(payload: BriefingBlockSave) {
    const name = briefingName;
    const previous = blocks;
    const response = await putBriefingBlock(payload);
    const saved = response.block;
    onChange(
      name,
      payload.id
        ? previous.map((block) => (block.id === saved.id ? saved : block))
        : [...previous, saved],
    );
    draft = null;
  }

  async function move(block: BriefingBlock, direction: number) {
    if (busy) return;
    const index = blocks.findIndex((item) => item.id === block.id);
    const next = index + direction;
    if (next < 0 || next >= blocks.length) return;
    const name = briefingName;
    const ordered = [...blocks];
    [ordered[index], ordered[next]] = [ordered[next], ordered[index]];
    busy = true;
    error = '';
    try {
      await putBriefingBlock({
        reorder: { briefing_name: name, ordered_ids: ordered.map((item) => item.id) },
      });
      onChange(
        name,
        ordered.map((item, position) => ({ ...item, position })),
      );
      announcement = `${block.title} moved to position ${next + 1} of ${ordered.length}`;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Could not save block order.';
    } finally {
      busy = false;
    }
  }

  async function duplicate(block: BriefingBlock) {
    if (busy) return;
    if (block.sources.length === 0) {
      error = 'Add a source to this block before duplicating it.';
      return;
    }
    busy = true;
    error = '';
    try {
      const name = briefingName;
      const payload = blockPayload(block, true);
      const previous = blocks;
      const response = await putBriefingBlock(payload);
      onChange(name, [...previous, response.block]);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Could not duplicate block.';
    } finally {
      busy = false;
    }
  }

  async function remove() {
    if (!deleting || busy) return;
    const id = deleting.id;
    const name = briefingName;
    const previous = blocks;
    busy = true;
    deleteError = '';
    try {
      await deleteBriefingBlock(id);
      onChange(
        name,
        previous.filter((block) => block.id !== id),
      );
      deleting = null;
    } catch (e) {
      deleteError = e instanceof Error ? e.message : 'Could not delete block.';
    } finally {
      busy = false;
    }
  }

  function menu(block: BriefingBlock, index: number): KebabItem[] {
    return [
      {
        label: 'Edit',
        disabled: busy,
        onSelect: () => {
          draft = blockPayload(block);
        },
      },
      {
        label: 'Move up',
        disabled: busy || index === 0,
        onSelect: () => {
          void move(block, -1);
        },
      },
      {
        label: 'Move down',
        disabled: busy || index === blocks.length - 1,
        onSelect: () => {
          void move(block, 1);
        },
      },
      {
        label: 'Duplicate',
        disabled: busy,
        onSelect: () => {
          void duplicate(block);
        },
      },
      {
        label: 'Delete',
        disabled: busy,
        danger: true,
        onSelect: () => {
          deleteError = '';
          deleting = block;
        },
      },
    ];
  }
</script>

<div class="block-builder">
  {#if error}<div role="alert" class="banner error">{error}</div>{/if}
  <div role="status" aria-live="polite" class="visually-hidden">{announcement}</div>
  {#if blocks.length === 0}
    <EmptyState size="sm">No blocks yet. Add a block to start this briefing.</EmptyState>
  {:else}
    <ol aria-label="Briefing blocks" class="block-list">
      {#each blocks as block, index (block.id)}
        <li class="block-row">
          <div class="block-content">
            <h3>{block.title}</h3>
            <p class="caption">{sourceSummary(block)}</p>
            <span class="caption">{widthLabel(block)}</span>
          </div>
          <KebabMenu items={menu(block, index)} ariaLabel={`Actions for ${block.title}`} />
        </li>
      {/each}
    </ol>
  {/if}
  <div class="builder-actions">
    <Button
      variant="secondary"
      onclick={() => {
        previewing = true;
      }}
      disabled={busy}>Preview</Button
    >
    <Button
      variant="secondary"
      onclick={() => {
        search = '';
        picking = true;
      }}
      disabled={busy}>Add block</Button
    >
  </div>
</div>

{#if picking}
  <Modal
    open={true}
    title="Add block"
    width="600px"
    onOpenChange={(next) => {
      picking = next;
    }}
  >
    <Input
      type="search"
      bind:value={search}
      aria-label="Search presets"
      placeholder="Search presets"
    />
    <ul class="preset-list">
      {#each filtered as template (template.id)}
        <li>
          <Button
            variant="secondary"
            onclick={() => choose(template)}
            disabled={!!unavailable(template)}>{template.label}</Button
          >
          <p class="caption">{template.description}</p>
          {#if unavailable(template)}<p class="caption">{unavailable(template)}</p>{/if}
        </li>
      {/each}
    </ul>
    {#if !filtered.length}<EmptyState size="sm">No matching presets.</EmptyState>{/if}
    {#snippet footer()}<Button
        variant="secondary"
        onclick={() => {
          picking = false;
        }}>Cancel</Button
      >{/snippet}
  </Modal>
{/if}

{#if draft}
  <BlockEditor
    initial={draft}
    savedBlocks={blocks}
    {sourceKinds}
    {browseOptions}
    {rssOptions}
    {sharedBlockOptions}
    {feedsAvailable}
    onSave={save}
    onCancel={() => {
      draft = null;
    }}
  />
{/if}

{#if previewing}
  <BriefingPreview
    {briefingName}
    {blocks}
    onClose={() => {
      previewing = false;
    }}
  />
{/if}

{#if deleting}
  <ConfirmDialog
    open={true}
    title="Delete block"
    confirmLabel={busy ? 'Deleting…' : 'Delete'}
    confirmDisabled={busy}
    onConfirm={() => {
      void remove();
    }}
    onCancel={() => {
      if (!busy) deleting = null;
    }}
  >
    {#snippet body()}
      <p>
        Are you sure you want to delete the block "{deleting?.title}"? Existing editions remain
        available.
      </p>
      {#if deleteError}<div role="alert" class="banner error">{deleteError}</div>{/if}
    {/snippet}
  </ConfirmDialog>
{/if}

<style>
  .block-builder {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .builder-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-start;
    gap: var(--space-2);
  }
  .block-list,
  .preset-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .block-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3);
    background: var(--surface-base);
  }
  .block-content {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  h3 {
    margin: 0 0 var(--space-1);
    font-size: var(--text-sm);
  }
  p {
    margin: var(--space-1) 0;
  }
  .preset-list {
    margin-top: var(--space-3);
  }
</style>
