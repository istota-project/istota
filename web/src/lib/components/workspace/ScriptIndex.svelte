<script lang="ts">
  import IndexCaption from './IndexCaption.svelte';
  import type { WorkspaceScriptsIndex } from '$lib/api';
  import {
    Button,
    EmptyState,
    KebabMenu,
    NoticeBanner,
    type KebabItem,
    PageLoading,
  } from '$lib/components/ui';
  import { formatDate, formatDateTime } from '$lib/dateFormat';
  import { brokenKind, scriptRefLabel, scriptUsage, type ScriptKind } from './scriptFiles';

  let {
    label,
    caption,
    reachNote = null,
    empty,
    kind,
    index,
    loading,
    error,
    onOpen,
    onRetry,
    revealAction,
  }: {
    label: string;
    caption: string;
    /** The bot icon's sentence for the whole list, when every entry shares one reach. */
    reachNote?: string | null;
    empty: string;
    kind: ScriptKind;
    index: WorkspaceScriptsIndex | null;
    loading: boolean;
    error: string;
    onOpen: (path: string) => void;
    onRetry: () => void;
    revealAction: (ref: string) => KebabItem;
  } = $props();

  let files = $derived(index?.files.filter((file) => file.kind === kind) ?? []);
  let broken = $derived(index?.broken.filter((item) => brokenKind(item) === kind) ?? []);
  let notes = $derived.by(() => {
    if (!index) return [];
    const out: string[] = [];
    if (index.truncated)
      out.push('Some files are not shown because the listing reached its limit.');
    if (index.sources.cron === 'error')
      out.push('CRON.md could not be read; references from it are not shown.');
    if (index.sources.heartbeat === 'error')
      out.push('HEARTBEAT.md could not be read; references from it are not shown.');
    if (index.sources.scripts === 'refused')
      out.push('The scripts folder could not be read safely.');
    return out;
  });
</script>

<section class="index" aria-label={label}>
  {#if error}
    <div class="center-msg error">
      <p>{error}</p>
      <Button variant="secondary" onclick={onRetry}>Retry</Button>
    </div>
  {:else if loading}
    <PageLoading />
  {:else if index}
    {#if broken.length}
      <div class="broken">
        <NoticeBanner title="Jobs refer to missing files" collapsed={false}>
          <ul class="list-reset">
            {#each broken as item (item.path)}
              <li>
                <strong>{item.path}</strong>: {item.referenced_by.map(scriptRefLabel).join(', ')}
              </li>
            {/each}
          </ul>
        </NoticeBanner>
      </div>
    {/if}
    {#if files.length}
      <IndexCaption {caption} {reachNote} />
      <ul class="list-reset">
        {#each files as file (file.path)}
          {@const viewPath = `${index.scripts_view_path}/${file.path}`}
          <li class="index-row">
            <div class="index-text">
              <button
                type="button"
                class="index-name button-reset truncate"
                title={file.path}
                onclick={() => onOpen(viewPath)}>{file.path}</button
              >
              <span class="caption">{file.description || 'No description'}</span>
              <span class="caption">{scriptUsage(file)}</span>
            </div>
            <time
              class="caption index-detail"
              datetime={file.modified}
              title={formatDateTime(file.modified)}>{formatDate(file.modified)}</time
            >
            <KebabMenu
              ariaLabel={`Actions for ${file.path}`}
              items={[{ label: 'Open', onSelect: () => onOpen(viewPath) }, revealAction(viewPath)]}
            />
          </li>
        {/each}
      </ul>
    {:else}
      <EmptyState>{empty}</EmptyState>
    {/if}
    {#each notes as note}
      <p class="caption index-note" role="status">{note}</p>
    {/each}
  {/if}
</section>

<style>
  .broken {
    padding: var(--space-3) var(--space-3) 0;
  }
  .broken ul {
    padding-left: var(--space-4);
    list-style: disc;
  }
  .index-note {
    margin: 0;
    padding: var(--space-3);
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
    color: var(--text-primary);
    text-align: left;
  }
  .index-name:hover {
    text-decoration: underline;
  }
  .index-detail {
    white-space: nowrap;
  }
</style>
