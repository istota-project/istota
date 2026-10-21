<script lang="ts">
  /**
   * A dialog that picks a folder in My files and returns its path relative to
   * My files (`Notes/Inbox`). It lists folders only, one level at a time, and
   * can make a new folder where it is. Files and hidden entries are not shown:
   * the answer is always a folder the user can see on the Files page.
   *
   * It opens at `start` when that folder can be listed, else at the root of
   * My files. The root itself cannot be chosen.
   */
  import { onDestroy } from 'svelte';
  import { Folder, FolderPlus } from '@lucide/svelte';
  import { createWorkspaceFolder, getWorkspaceListing, type WorkspaceEntry } from '$lib/api';
  import {
    Button,
    EmptyState,
    Field,
    FormStack,
    Icon,
    Input,
    ListRow,
    LoadState,
    Modal,
  } from '@minot/ui';
  import { latest } from '@minot/ui/ui/latest';

  interface Props {
    open: boolean;
    /** Where to open: a path relative to My files. Null or empty is the root. */
    start?: string | null;
    title?: string;
    description?: string;
    chooseLabel?: string;
    onPick: (path: string) => void;
  }

  let {
    open = $bindable(false),
    start = null,
    title = 'Choose a folder',
    description,
    chooseLabel = 'Choose this folder',
    onPick,
  }: Props = $props();

  let path = $state('');
  let folders: WorkspaceEntry[] = $state([]);
  let truncated = $state(false);
  let loading = $state(false);
  let loadError = $state('');

  let creating = $state(false);
  let newName = $state('');
  let createError = $state('');
  let createBusy = $state(false);

  const loads = latest();
  onDestroy(() => loads.dispose());

  const parts = $derived(path ? path.split('/') : []);

  function clean(value: string | null | undefined): string {
    return (value ?? '').split('/').filter(Boolean).join('/');
  }

  async function show(next: string, fallBackToRoot = false) {
    const current = loads.next();
    path = next;
    loading = true;
    loadError = '';
    creating = false;
    try {
      const listing = await getWorkspaceListing('home', next);
      if (!current()) return;
      folders = listing.entries.filter((entry) => entry.kind === 'dir' && !entry.hidden);
      truncated = listing.truncated;
    } catch (e) {
      if (!current()) return;
      // A stored folder that does not exist yet is a valid setting; open at
      // the root rather than on an error the user cannot act on.
      if (fallBackToRoot && next) {
        void show('');
        return;
      }
      folders = [];
      loadError = (e as Error).message || 'Could not list this folder.';
    } finally {
      if (current()) loading = false;
    }
  }

  // Each time the dialog opens, it starts again from `start`.
  let wasOpen = false;
  $effect(() => {
    const isOpen = open;
    if (isOpen && !wasOpen) void show(clean(start), true);
    if (!isOpen) loads.invalidate();
    wasOpen = isOpen;
  });

  function child(name: string): string {
    return path ? `${path}/${name}` : name;
  }

  function startCreate() {
    newName = '';
    createError = '';
    creating = true;
  }

  async function createFolder() {
    const name = newName.trim();
    if (!name || createBusy) return;
    createBusy = true;
    createError = '';
    try {
      const made = await createWorkspaceFolder(path, name);
      await show(clean(made.path) || child(name));
    } catch (e) {
      createError = (e as Error).message || 'Could not make the folder.';
    } finally {
      createBusy = false;
    }
  }

  function choose() {
    if (!path) return;
    onPick(path);
    open = false;
  }
</script>

<Modal bind:open {title} {description} height="100dvh" busy={createBusy}>
  <div class="vstack folder-picker">
    <nav class="hstack wrap crumbs" aria-label="Folder path">
      {#if parts.length === 0}
        <span aria-current="location">My files</span>
      {:else}
        <button type="button" class="link-action" onclick={() => show('')}>My files</button>
      {/if}
      {#each parts as part, i (i)}
        <span class="muted" aria-hidden="true">/</span>
        {#if i === parts.length - 1}
          <span aria-current="location">{part}</span>
        {:else}
          <button
            type="button"
            class="link-action"
            onclick={() => show(parts.slice(0, i + 1).join('/'))}>{part}</button
          >
        {/if}
      {/each}
    </nav>

    <LoadState
      scope="card"
      {loading}
      error={loadError}
      onRetry={() => show(path)}
      empty={folders.length === 0}
    >
      {#snippet emptyState()}
        <EmptyState size="sm">No folders here.</EmptyState>
      {/snippet}
      <div class="folder-list" role="list" aria-label="Folders">
        {#each folders as folder (folder.name)}
          {#snippet folderIcon()}<Icon icon={Folder} size="sm" />{/snippet}
          <div role="listitem">
            <ListRow density="sm" icon={folderIcon} onclick={() => show(child(folder.name))}>
              {folder.name}
            </ListRow>
          </div>
        {/each}
      </div>
      {#if truncated}
        <p class="caption">Not every folder here is shown.</p>
      {/if}
    </LoadState>

    {#if creating}
      <FormStack as="form" onsubmit={createFolder} disabled={createBusy}>
        <Field label="New folder name" error={createError}>
          <Input bind:value={newName} autocomplete="off" />
        </Field>
        <div class="hstack">
          <Button
            type="submit"
            variant="primary"
            size="sm"
            disabled={!newName.trim()}
            loading={createBusy}
            loadingLabel="Creating…">Create</Button
          >
          <Button variant="secondary" size="sm" onclick={() => (creating = false)}>Cancel</Button>
        </div>
      </FormStack>
    {:else if !loadError}
      <div>
        <Button variant="secondary" size="sm" icon={FolderPlus} onclick={startCreate}>
          New folder
        </Button>
      </div>
    {/if}
  </div>

  {#snippet footer()}
    {#if !path}
      <span class="caption picker-note">Open a folder to choose it.</span>
    {/if}
    <Button variant="secondary" onclick={() => (open = false)}>Cancel</Button>
    <Button variant="primary" disabled={!path || !!loadError || loading} onclick={choose}>
      {chooseLabel}
    </Button>
  {/snippet}
</Modal>

<style>
  .crumbs {
    gap: var(--space-1);
    overflow-wrap: anywhere;
  }

  .folder-list {
    display: flex;
    flex-direction: column;
  }

  .picker-note {
    align-self: center;
    margin-right: auto;
  }
</style>
