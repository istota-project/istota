<script lang="ts">
  import IndexCaption from './IndexCaption.svelte';
  import { Button, EmptyState, KebabMenu, type KebabItem, PageLoading } from '$lib/components/ui';
  import type { WorkspaceConfigFile } from '$lib/api';
  import { configFileDescription, configFileDetail, configFileLabel } from './configFiles';

  let {
    label,
    caption,
    reachNote = null,
    empty,
    files,
    loading,
    error,
    onEdit,
    onRetry,
    revealAction,
  }: {
    label: string;
    caption: string;
    /** The bot icon's sentence for the whole list, when every entry shares one reach. */
    reachNote?: string | null;
    empty: string;
    files: WorkspaceConfigFile[];
    loading: boolean;
    error: string;
    onEdit: (file: WorkspaceConfigFile) => void;
    onRetry: () => void;
    revealAction: (
      ref: string | null | undefined,
      provider?: 'workspace' | 'drive',
      available?: boolean,
    ) => KebabItem;
  } = $props();
</script>

<section class="index" aria-label={label}>
  {#if error}
    <div class="center-msg error">
      <p>{error}</p>
      <Button variant="secondary" onclick={onRetry}>Retry</Button>
    </div>
  {:else if loading}
    <PageLoading />
  {:else if files.length}
    <IndexCaption {caption} {reachNote} />
    <ul>
      {#each files as file (file.name)}
        <li class="index-row">
          <div class="index-text">
            <button
              type="button"
              class="index-name"
              disabled={!file.editable}
              title={file.name}
              onclick={() => onEdit(file)}>{configFileLabel(file)}</button
            >
            <span class="caption">{configFileDescription(file)}</span>
          </div>
          <span class="caption index-detail">{configFileDetail(file)}</span>
          <KebabMenu
            ariaLabel={`Actions for ${file.name}`}
            items={[
              { label: 'Edit', disabled: !file.editable, onSelect: () => onEdit(file) },
              revealAction(file.view_path, 'workspace', file.exists),
            ]}
          />
        </li>
      {/each}
    </ul>
  {:else}
    <EmptyState>{empty}</EmptyState>
  {/if}
</section>

<style>
  .index ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .index-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  .index-text {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
  }
  .index-name {
    align-self: flex-start;
    max-width: 100%;
    padding: 0;
    border: 0;
    background: none;
    font: inherit;
    line-height: inherit;
    color: var(--text-primary);
    text-align: left;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: pointer;
  }
  .index-name:hover:not(:disabled) {
    text-decoration: underline;
  }
  .index-name:disabled {
    color: var(--text-dim);
    cursor: default;
  }
  .index-detail {
    white-space: nowrap;
  }
</style>
