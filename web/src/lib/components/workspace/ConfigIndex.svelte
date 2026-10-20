<script lang="ts">
  import IndexCaption from './IndexCaption.svelte';
  import { EmptyState, KebabMenu, type KebabItem, LoadState } from '$lib/components/ui';
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
  <LoadState {loading} {error} {onRetry} empty={!files.length}>
    {#snippet emptyState()}
      <EmptyState>{empty}</EmptyState>
    {/snippet}
    <IndexCaption {caption} {reachNote} />
    <ul class="list-reset">
      {#each files as file (file.name)}
        <li class="record-row record-row--divided index-row">
          <div class="index-text">
            <button
              type="button"
              class="index-name button-reset truncate"
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
  </LoadState>
</section>

<style>
  /* A record row (table.css), ruled like the file table beside it. */
  .index-row {
    align-items: center;
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
    color: var(--text-primary);
    text-align: left;
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
