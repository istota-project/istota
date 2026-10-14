<script lang="ts">
  import { tick } from 'svelte';
  import type { WorkspaceScriptsIndex, WorkspaceScriptRef } from '$lib/api';
  import { Button, EmptyState, KebabMenu, NoticeBanner, type KebabItem } from '$lib/components/ui';
  import { formatDate, formatDateTime } from '$lib/dateFormat';

  let {
    index = null,
    loading = false,
    error = '',
    selected,
    onSelect,
    onOpen,
    onRetry,
    revealAction,
  }: {
    index?: WorkspaceScriptsIndex | null;
    loading?: boolean;
    error?: string;
    selected?: string;
    onSelect: (path?: string) => void;
    onOpen: (path: string) => void;
    onRetry: () => void;
    revealAction: (ref: string) => KebabItem;
  } = $props();
  let root: HTMLElement;
  let groups = $derived([
    { label: 'Scripts', files: index?.files.filter((file) => file.kind === 'script') ?? [] },
    { label: 'Prompt files', files: index?.files.filter((file) => file.kind === 'prompt') ?? [] },
  ]);

  $effect(() => {
    const path = selected;
    const files = index?.files;
    if (loading || error || !path || !files?.some((file) => file.path === path)) return;
    let cancelled = false;
    void tick().then(() => {
      if (cancelled) return;
      const row = Array.from(root.querySelectorAll<HTMLElement>('[data-script]')).find(
        (node) => node.dataset.script === path,
      );
      row?.scrollIntoView?.({ block: 'nearest' });
    });
    return () => {
      cancelled = true;
    };
  });
</script>

{#snippet reference(ref: WorkspaceScriptRef, full = false)}
  {ref.source}: {ref.name}{#if full || ref.state !== 'enabled'}
    {' · '}{ref.state}{/if}{#if full}
    {' · field: '}{ref.field} · kind: {ref.kind}{/if}
{/snippet}

<section class="scripts-view" aria-label="Scripts view" bind:this={root}>
  {#if error}
    <div class="center-msg error">
      <p>{error}</p>
      <Button variant="secondary" onclick={onRetry}>Retry</Button>
    </div>
  {:else if loading}
    <div class="center-msg">Loading scripts…</div>
  {:else if index}
    {#if index.broken.length}
      <div class="broken-notice">
        <NoticeBanner title="Broken script references" collapsed={false}>
          <ul>
            {#each index.broken as broken (broken.path)}
              <li>
                <strong>{broken.path}</strong>
                <ul>
                  {#each broken.referenced_by as ref}<li>{@render reference(ref, true)}</li>{/each}
                </ul>
              </li>
            {/each}
          </ul>
        </NoticeBanner>
      </div>
    {/if}
    {#if !index.files.length}<EmptyState>No scripts yet</EmptyState>{/if}
    {#each groups as group (group.label)}
      {#if group.files.length}
        <div class="table-scroll">
          <table aria-label={group.label}>
            <caption>{group.label}</caption>
            <thead
              ><tr>
                <th scope="col">Name</th><th scope="col">Description</th>
                <th scope="col">Used by</th><th scope="col" class="modified">Modified</th>
                <th scope="col" class="actions-column" aria-label="Actions"></th>
              </tr></thead
            >
            <tbody>
              {#each group.files as file (file.path)}
                {@const expanded = selected === file.path}
                {@const refs = [...file.referenced_by, ...file.mentioned_in]}
                {@const viewPath = `${index.scripts_view_path}/${file.path}`}
                <tr data-script={file.path} class:expanded>
                  <th scope="row"
                    ><button
                      type="button"
                      class="script-name touch-target"
                      aria-expanded={expanded}
                      onclick={() => onSelect(expanded ? undefined : file.path)}>{file.path}</button
                    ></th
                  >
                  <td
                    ><span class:muted={!file.description}
                      >{file.description || 'No description'}</span
                    ></td
                  >
                  <td>
                    {#each file.referenced_by as ref}<div>{@render reference(ref)}</div>{:else}<span
                        class="muted">Not referenced</span
                      >{/each}
                    {#if file.mentioned_in.length}<div class="muted">
                        mentioned in {file.mentioned_in.length}
                        {file.mentioned_in.length === 1 ? 'prompt' : 'prompts'}
                      </div>{/if}
                  </td>
                  <td class="modified muted"
                    ><time datetime={file.modified} title={formatDateTime(file.modified)}
                      >{formatDate(file.modified)}</time
                    ></td
                  >
                  <td class="actions-column"
                    ><KebabMenu
                      ariaLabel={`Actions for ${file.path}`}
                      items={[
                        { label: 'Open', onSelect: () => onOpen(viewPath) },
                        revealAction(viewPath),
                      ]}
                    /></td
                  >
                </tr>
                {#if expanded}
                  <tr class="reference-row"
                    ><td colspan="5">
                      <section aria-label={`References for ${file.path}`}>
                        {#if refs.length}<ul>
                            {#each refs as ref}<li>{@render reference(ref, true)}</li>{/each}
                          </ul>
                        {:else}<span class="muted">No references or prompt mentions.</span>{/if}
                      </section>
                    </td></tr
                  >
                {/if}
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {/each}
    <div class="listing-notes muted" role="status">
      {#if index.truncated}<p>
          Some files are not shown because the scripts listing reached its limit.
        </p>{/if}
      {#each ['cron', 'heartbeat'] as const as source}
        {#if index.sources[source] === 'error'}<p>
            {source === 'cron' ? 'CRON.md' : 'HEARTBEAT.md'} could not be read; references from it are
            not shown.
          </p>{/if}
      {/each}
      {#if index.sources.scripts === 'refused'}<p>
          The scripts folder could not be read safely.
        </p>{/if}
    </div>
  {/if}
</section>

<style>
  .broken-notice {
    padding: var(--space-3);
  }
  ul {
    margin: 0;
    padding-left: var(--space-4);
  }
  .table-scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }
  caption {
    text-align: left;
    font-weight: 600;
    padding: var(--space-3);
  }
  th,
  td {
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    text-align: left;
    vertical-align: top;
    overflow-wrap: anywhere;
  }
  thead th {
    font-weight: 500;
    color: var(--text-dim);
    font-size: var(--text-xs);
  }
  tbody th {
    font-weight: normal;
  }
  .script-name {
    background: none;
    border: 0;
    padding: 0;
    color: var(--text-primary);
    font: inherit;
    text-align: left;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .script-name:hover {
    text-decoration: underline;
  }
  .expanded,
  .reference-row {
    background: var(--surface-raised);
  }
  .modified {
    white-space: nowrap;
  }
  .actions-column {
    width: 3rem;
    text-align: center;
    padding-inline: var(--space-1);
  }
  .listing-notes {
    padding: var(--space-3);
    font-size: var(--text-sm);
  }
  .listing-notes p {
    margin: 0 0 var(--space-1);
  }
  @media (max-width: 640px) {
    th,
    td {
      padding: var(--space-2);
    }
    .modified {
      display: none;
    }
  }
</style>
