<script lang="ts">
  import { onMount } from 'svelte';
  import { Button, EmptyState, NoticeBanner } from '$lib/components/ui';
  import TextFileEditor from '$lib/components/TextFileEditor.svelte';
  import TextPreview from '$lib/components/viewer/TextPreview.svelte';
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
    if (e.code === 'memory_unreadable' || e.status === 404 || e.status === 403) {
      saveBlocked = true;
      entries = [];
      cursor = null;
      notebook = null;
      error = e.message;
    }
    return e.message;
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
  <NoticeBanner variant="info" title={charter} />
  {#if loading}
    <p class="center-msg">Loading notebook…</p>
  {:else if error}
    <p class="center-msg error">{error}</p>
    <div class="actions">
      <Button onclick={loadNotebook}>Reload notebook</Button>
      {#if user.is_admin && storageUnavailable}<Button onclick={retryStorage}
          >Retry storage setup</Button
        >{/if}
    </div>
  {:else if notebook}
    <div class="actions">
      <Button onclick={() => (editorOpen = true)}>Edit notebook</Button>
      <Button onclick={openActivity}>Recorded changes</Button>
    </div>
    {#if notebook.large}<NoticeBanner variant="warn" title="Large notebook" collapsed={false}
        >Keep the notebook concise so shared facts remain easy to find.</NoticeBanner
      >{/if}
    {#if auditWarning}<NoticeBanner variant="warn" title={auditWarning} />{/if}
    <p class="caption">
      {notebook.bytes.toLocaleString()} bytes{#if notebook.last_change}
        · Last recorded change: {notebook.last_change.user_id || 'Unknown actor'} · {notebook
          .last_change.ts || 'Unknown time'}{/if}
    </p>
    {#if notebook.content.trim()}<TextPreview name="GROUP.md" text={notebook.content} untrusted />
    {:else}<EmptyState size="sm"
        >The shared notebook is empty. Add facts you want the group to remember.</EmptyState
      >{/if}
  {/if}
  {#if activityOpen}
    <section aria-label="Recorded changes">
      <h2>Recorded changes</h2>
      <p class="caption">
        Direct file edits may not appear here. These records are not restorable versions.
      </p>
      {#each entries as entry (entry.id)}
        <article>
          <p>
            {entry.user_id || 'Unknown actor'} · {entry.ts || 'Unknown time'}{#if entry.source}
              · {entry.source}{/if}
          </p>
          <ul>
            {#each entry.applied ?? [] as outcome}<li>
                {outcome.op?.op || 'change'}: {outcome.outcome || 'applied'}
              </li>{/each}
            {#each entry.rejected ?? [] as outcome}<li>
                {outcome.op?.op || 'change'}: {outcome.outcome || 'rejected'}{#if outcome.reason}
                  — {outcome.reason}{/if}
              </li>{/each}
          </ul>
        </article>
      {/each}
      {#if activityError}<p class="banner error">{activityError}</p>
        <Button onclick={() => loadActivity()}>Retry recorded changes</Button>
      {:else if !entries.length && !activityBusy}<EmptyState size="sm"
          >No recorded changes yet.</EmptyState
        >{/if}
      {#if activityBusy}<p class="caption">Loading recorded changes…</p>
      {:else if cursor}<Button onclick={() => loadActivity(true)}>Load older changes</Button>{/if}
    </section>
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
    flex-direction: column;
    gap: var(--space-3);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  h2 {
    font-size: var(--text-lg);
  }
  article {
    border-bottom: 1px solid var(--border-subtle);
    padding-block: var(--space-2);
  }
</style>
