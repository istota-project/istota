<script lang="ts">
  import { onMount } from 'svelte';
  import { Button, EmptyState, NoticeBanner, Card, LoadMore } from '$lib/components/ui';
  import TextFileEditor from '$lib/components/TextFileEditor.svelte';
  import TextPreview from '$lib/components/viewer/TextPreview.svelte';
  import { formatBytes } from '$lib/format';
  import { formatDateTime, formatRelative } from '$lib/dateFormat';
  import {
    getGroupMemory,
    saveGroupMemory,
    getGroupMemoryActivity,
    retryGroupStorage,
    GroupApiError,
    AuthError,
    type GroupMemory,
    type GroupMemoryChange,
  } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';

  let { groupId }: { groupId: string } = $props();
  const current = getCurrentUser();
  const user = $derived(current.user);
  let notebook = $state<GroupMemory | null>(null);
  let error = $state('');
  let loading = $state(true);
  let editorOpen = $state(false);
  let saveBlocked = $state(false);
  let activityOpen = $state(false);
  let entries = $state<GroupMemoryChange[]>([]);
  let cursor = $state<string | null>(null);
  let activityBusy = $state(false);
  let activityError = $state('');
  let auditWarning = $state('');
  let storageUnavailable = $state(false);
  let active = true;
  const charter = 'Everything shared here is available to all current and future group members.';

  function describeError(e: unknown): string | null {
    if (e instanceof AuthError) {
      current.expireSession();
      return 'Your session has expired.';
    }
    if (!(e instanceof GroupApiError)) return null;
    storageUnavailable = e.reason === 'storage_unavailable' || e.code === 'memory_unavailable';
    const message = storageUnavailable
      ? 'Group storage is unavailable. Notebook editing is disabled until storage is restored.'
      : e.message;
    if (e.code === 'memory_unreadable' || e.status === 404 || e.status === 403) {
      saveBlocked = true;
      entries = [];
      cursor = null;
      notebook = null;
      error = message;
    }
    return message;
  }
  async function loadNotebook() {
    loading = true;
    error = '';
    notebook = null;
    try {
      const result = await getGroupMemory(groupId);
      if (active) notebook = result;
    } catch (e) {
      if (active) error = describeError(e) ?? 'Could not load the notebook.';
    } finally {
      if (active) loading = false;
    }
  }
  async function loadEditor(key: string | number) {
    saveBlocked = false;
    const result = await getGroupMemory(String(key));
    return { ...result, exists: result.status === 'readable' };
  }
  async function saveEditor(key: string | number, content: string, revision: string) {
    const result = await saveGroupMemory(String(key), content, revision);
    auditWarning = result.audit_recorded ? '' : 'Saved, but this change could not be recorded.';
    return { revision: result.revision, note: auditWarning || 'Saved.' };
  }
  function closeEditor() {
    editorOpen = false;
    loadNotebook();
    if (activityOpen) loadActivity();
  }
  async function loadActivity(older = false) {
    if (activityBusy) return;
    activityBusy = true;
    activityError = '';
    try {
      const result = await getGroupMemoryActivity(
        groupId,
        older ? (cursor ?? undefined) : undefined,
      );
      if (!active) return;
      entries = older ? [...entries, ...result.entries] : result.entries;
      cursor = result.next_cursor;
    } catch (e) {
      if (active) activityError = describeError(e) ?? 'Could not load recorded changes.';
    } finally {
      if (active) activityBusy = false;
    }
  }
  function openActivity() {
    activityOpen = !activityOpen;
    if (activityOpen) loadActivity();
  }
  async function retryStorage() {
    loading = true;
    error = '';
    try {
      await retryGroupStorage(groupId);
      await loadNotebook();
    } catch (e) {
      error = describeError(e) ?? 'Storage is still unavailable.';
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    loadNotebook();
    return () => {
      active = false;
    };
  });
</script>

<div class="notebook">
  {#if notebook?.large}<NoticeBanner variant="warn" title="Large notebook" collapsed={false}
      >Keep the notebook concise so shared facts remain easy to find.</NoticeBanner
    >{/if}
  {#if auditWarning}<NoticeBanner variant="warn" title={auditWarning} />{/if}
  <Card title="Notebook" description={charter}>
    {#snippet status()}{#if notebook}<span class="caption">{formatBytes(notebook.bytes)}</span
        >{/if}{/snippet}
    {#snippet actions()}{#if notebook}
        <Button size="sm" onclick={openActivity}>Recorded changes</Button>
        <Button size="sm" variant="primary" onclick={() => (editorOpen = true)}
          >Edit notebook</Button
        >
      {/if}{/snippet}
    {#if loading}<p class="caption">Loading notebook…</p>
    {:else if error}
      <p class="banner error">{error}</p>
      <div class="actions">
        <Button size="sm" onclick={loadNotebook}>Reload notebook</Button>
        {#if user.is_admin && storageUnavailable}<Button size="sm" onclick={retryStorage}
            >Retry storage setup</Button
          >{/if}
      </div>
    {:else if notebook}
      {#if notebook.content.trim()}<div class="reading">
          <TextPreview name="GROUP.md" text={notebook.content} untrusted />
        </div>
      {:else}<EmptyState size="sm"
          >The shared notebook is empty. Add facts you want the group to remember.</EmptyState
        >{/if}
      {#if notebook.last_change}<p class="caption last-change">
          Last changed by {notebook.last_change.user_id ||
            'an unknown actor'}{#if notebook.last_change.ts}
            <span title={formatDateTime(notebook.last_change.ts)}
              >{formatRelative(notebook.last_change.ts)}</span
            >{/if}
        </p>{/if}
    {/if}
  </Card>
  {#if activityOpen}
    <Card
      title="Recorded changes"
      description="Direct file edits may not appear here. These records are not restorable versions."
    >
      {#if entries.length}
        <ol class="list-reset">
          {#each entries as entry (entry.id)}
            <li class="change">
              <div class="change-head">
                <span class="change-actor">{entry.user_id || 'Unknown actor'}</span>
                <span class="caption" title={formatDateTime(entry.ts)}
                  >{entry.ts ? formatRelative(entry.ts) : 'Unknown time'}{#if entry.source}
                    · {entry.source}{/if}</span
                >
              </div>
              <ul class="change-outcomes muted">
                {#each entry.applied ?? [] as outcome}<li>
                    {outcome.op?.op || 'change'}: {outcome.outcome || 'applied'}
                  </li>{/each}
                {#each entry.rejected ?? [] as outcome}<li class="rejected">
                    {outcome.op?.op || 'change'}: {outcome.outcome ||
                      'rejected'}{#if outcome.reason}
                      — {outcome.reason}{/if}
                  </li>{/each}
              </ul>
            </li>
          {/each}
        </ol>
      {/if}
      {#if activityError}<p class="banner error">{activityError}</p>
        <div class="actions">
          <Button size="sm" onclick={() => loadActivity()}>Retry recorded changes</Button>
        </div>
      {:else if !entries.length && !activityBusy}<EmptyState size="sm"
          >No recorded changes yet.</EmptyState
        >{/if}
      {#if activityBusy && !entries.length}<p class="caption">Loading recorded changes…</p>
      {:else}<LoadMore
          onload={() => loadActivity(true)}
          hasMore={!!cursor}
          loading={activityBusy}
          label="Load older changes"
        />{/if}
    </Card>
  {/if}
</div>
<TextFileEditor
  bind:open={editorOpen}
  fileKey={groupId}
  title="Edit group notebook"
  description={charter}
  ariaLabel="Group notebook"
  loadFailed="Could not load the notebook."
  load={loadEditor}
  save={saveEditor}
  saveDisabled={saveBlocked}
  {describeError}
  onClose={closeEditor}
/>

<style>
  .notebook {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: var(--space-3);
  }
  /* Takes only spare height, so on the overview the card fills a grid row a
     taller neighbour set, and anywhere else it keeps its natural size. */
  .notebook > :global(.ui-card) {
    flex-grow: 1;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  /* Capped so a long notebook scrolls in its card rather than pushing every
     other card on the overview off the screen. */
  .reading {
    max-height: 28rem;
    overflow: auto;
    padding: var(--space-4);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-base);
  }
  .reading :global(.markdown > :first-child) {
    margin-top: 0;
  }
  .reading :global(.markdown > :last-child) {
    margin-bottom: 0;
  }
  .last-change {
    margin: var(--space-2) 0 0;
  }
  .change {
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
    overflow-wrap: anywhere;
  }
  .change:last-child {
    border-bottom: none;
  }
  .change-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: var(--space-3);
  }
  .change-actor {
    font-weight: var(--weight-medium);
  }
  .change-outcomes {
    margin: var(--space-1) 0 0;
    padding-left: var(--space-4);
    font-size: var(--text-sm);
  }
  .change-outcomes .rejected {
    color: var(--status-danger-fg);
  }
</style>
