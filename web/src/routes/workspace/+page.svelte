<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    Button,
    Modal,
    Field,
    Input,
    FileDropZone,
  } from '$lib/components/ui';
  import {
    getWorkspacePlaces,
    getWorkspaceListing,
    createWorkspaceFolder,
    uploadWorkspaceFile,
    type WorkspacePlace,
    type WorkspaceListing,
  } from '$lib/api';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import { loadSetting, saveSetting } from '$lib/stores/persisted';
  import PlaceList from '$lib/components/workspace/PlaceList.svelte';
  import ReachPanel from '$lib/components/workspace/ReachPanel.svelte';
  import FileTable from '$lib/components/workspace/FileTable.svelte';

  type Selection = { place: string; path: string };
  let places = $state<WorkspacePlace[]>([]);
  let notes = $state<string[]>([]);
  let loading = $state(true);
  let error = $state('');
  let sidebarOpen = $state(false);
  let selected = $state<Selection | null>(null);
  let place = $derived(places.find((item) => item.id === selected?.place));
  let listing = $state<WorkspaceListing | null>(null);
  let listingLoading = $state(false);
  let listingError = $state('');
  let showHidden = $state(loadSetting<boolean>('workspace.showHidden', false) === true);
  let fileInput: HTMLInputElement;
  let folderOpen = $state(false);
  let folderName = $state('');
  let folderPath = $state('');
  let folderBusy = $state(false);
  let folderError = $state('');
  type Upload = {
    file: File;
    path: string;
    progress: number;
    state: 'waiting' | 'uploading' | 'done' | 'error';
    message: string;
  };
  let uploads = $state<Upload[]>([]);
  let uploading = false;

  function openFolderDialog() {
    folderPath = selected?.path || '';
    folderName = '';
    folderError = '';
    folderOpen = true;
  }
  async function refreshIfCurrent(path: string) {
    if (active && selected?.place === 'home' && selected.path === path) await loadFolder(selected);
  }
  async function createFolder(event: SubmitEvent) {
    event.preventDefault();
    if (!folderName.trim() || folderBusy) return;
    folderBusy = true;
    folderError = '';
    try {
      await createWorkspaceFolder(folderPath, folderName.trim());
      folderOpen = false;
      await refreshIfCurrent(folderPath);
    } catch (e) {
      folderError = e instanceof Error ? e.message : 'Could not create this folder.';
    } finally {
      folderBusy = false;
    }
  }
  function enqueue(files: File[]) {
    if (selected?.place !== 'home') return;
    uploads.push(
      ...files.map((file): Upload => ({
        file,
        path: selected!.path,
        progress: 0,
        state: 'waiting',
        message: '',
      })),
    );
    void runUploads();
  }
  async function runUploads() {
    if (uploading) return;
    uploading = true;
    try {
      for (const row of uploads) {
        if (!active || row.state !== 'waiting') continue;
        row.state = 'uploading';
        try {
          const result = await uploadWorkspaceFile(
            row.path,
            row.file,
            (percent) => (row.progress = percent),
          );
          row.state = 'done';
          row.message = result.name === row.file.name ? 'Uploaded' : `Saved as ${result.name}`;
          await refreshIfCurrent(row.path);
        } catch (e) {
          row.state = 'error';
          row.message = e instanceof Error ? e.message : 'Could not upload this file.';
        }
      }
    } finally {
      uploading = false;
    }
  }
  let generation = 0;
  let active = true;
  const selection = createUrlSelection<Selection>({
    key: 'workspace',
    params: ['place', 'path'],
    encode: (value) => ({ place: value.place, ...(value.path ? { path: value.path } : {}) }),
    decode: (params) => ({
      place: params.place || places[0]?.id || 'home',
      path: params.path || '',
    }),
    read: () => selected,
    apply: loadFolder,
  });
  selection.start();

  function href(place: string, path = '') {
    return `${base}/workspace?${new URLSearchParams({ place, ...(path ? { path } : {}) })}`;
  }
  function navigate(event: MouseEvent, place: string, path = '') {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
      return;
    event.preventDefault();
    sidebarOpen = false;
    selection.push({ place, path });
  }
  async function loadFolder(value: Selection) {
    const request = ++generation;
    selected = value;
    listing = null;
    listingError = '';
    listingLoading = false;
    if (!places.find((item) => item.id === value.place)?.browsable) return;
    listingLoading = true;
    try {
      const result = await getWorkspaceListing(value.place, value.path);
      if (request !== generation) return;
      listing = result;
      selected = { place: result.place, path: result.path };
    } catch (e) {
      if (request === generation)
        listingError =
          e instanceof Error && !(e instanceof TypeError)
            ? e.message
            : 'Could not load this folder.';
    } finally {
      if (request === generation) listingLoading = false;
    }
  }
  async function load() {
    loading = true;
    error = '';
    try {
      const result = await getWorkspacePlaces();
      if (!active) return;
      places = result.places;
      notes = result.notes;
      if (places.length) void loadFolder(selection.current()!);
    } catch {
      error = 'Could not load your workspace.';
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    void load();
    return () => {
      active = false;
      generation++;
    };
  });
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title="Workspace"
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open places"
    >
      {#snippet leading()}<SidebarToggle
          open={sidebarOpen}
          label="Places"
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />{/snippet}
    </ShellHeader>
  {/snippet}
  {#snippet sidebar()}
    {#if places.length}
      <Sidebar
        title="Places"
        open={sidebarOpen}
        onClose={() => (sidebarOpen = false)}
        width="260px"
      >
        <PlaceList {places} selected={selected?.place || ''} {href} onNavigate={navigate} />
      </Sidebar>
    {/if}
  {/snippet}
  {#if loading}
    <div class="center-msg">Loading workspace…</div>
  {:else if error}
    <div class="center-msg error">
      <p>{error}</p>
      <Button variant="secondary" onclick={load}>Retry</Button>
    </div>
  {:else if !places.length}
    <div class="center-msg">No workspace files or connections are available.</div>
  {:else if !place}
    <div class="center-msg">This place is no longer available.</div>
  {:else}
    <div class="folder-toolbar">
      <nav aria-label="Folder path">
        <ol>
          <li>
            <a
              href={href(place.id)}
              onclick={(event) => navigate(event, place.id)}
              aria-current={!selected?.path ? 'page' : undefined}>{place.label}</a
            >
          </li>
          {#each (selected?.path || '').split('/').filter(Boolean) as part, i}
            {@const parts = selected!.path.split('/').filter(Boolean)}
            <li>
              <span class="muted" aria-hidden="true">/</span><a
                href={href(place.id, parts.slice(0, i + 1).join('/'))}
                onclick={(event) => navigate(event, place!.id, parts.slice(0, i + 1).join('/'))}
                aria-current={i === parts.length - 1 ? 'page' : undefined}>{part}</a
              >
            </li>
          {/each}
        </ol>
      </nav>
      <div class="folder-actions">
        {#if place.id === 'home' && listing && !listingLoading && !listingError}
          <Button variant="secondary" size="sm" onclick={openFolderDialog}>New folder</Button>
          <Button size="sm" onclick={() => fileInput.click()}>Upload</Button>
          <input
            bind:this={fileInput}
            type="file"
            multiple
            hidden
            aria-label="Upload files"
            onchange={() => {
              enqueue(Array.from(fileInput.files || []));
              fileInput.value = '';
            }}
          />
        {/if}
        {#if place.browsable}
          <label class="hidden-toggle"
            ><input
              type="checkbox"
              bind:checked={showHidden}
              onchange={() => saveSetting('workspace.showHidden', showHidden)}
            />Show hidden</label
          >
        {/if}
      </div>
    </div>
    <ReachPanel {place} {notes} />
    {#if uploads.length}
      <section class="uploads" aria-label="Uploads">
        <div class="upload-heading">
          <span class="micro-label">Uploads</span>
          {#if !uploads.some((row) => row.state === 'waiting' || row.state === 'uploading')}
            <Button size="sm" variant="ghost" onclick={() => (uploads = [])}>Dismiss</Button>
          {/if}
        </div>
        <ul>
          {#each uploads as row}
            <li class="upload-row">
              <span class="upload-name"
                >{row.file.name}<span class="caption"
                  >My files{row.path ? ` / ${row.path}` : ''}</span
                ></span
              >
              <span
                class:form-error={row.state === 'error'}
                class="upload-status"
                aria-live="polite"
              >
                {row.state === 'waiting'
                  ? 'Waiting'
                  : row.state === 'uploading'
                    ? row.progress === 100
                      ? 'Saving…'
                      : `${row.progress}%`
                    : row.message}
              </span>
              {#if row.state === 'uploading'}<progress
                  max="100"
                  value={row.progress}
                  aria-label={`Uploading ${row.file.name}`}
                ></progress>{/if}
            </li>
          {/each}
        </ul>
      </section>
    {/if}
    <FileDropZone
      overlay
      disabled={place.id !== 'home' || !listing || listingLoading || !!listingError}
      onFiles={enqueue}
    >
      {#if !place.browsable}
        <div class="center-msg">
          {place.kind === 'talk'
            ? 'Talk attachments are available to tasks, but cannot be browsed here.'
            : 'This connection is reached through a skill during a task.'}
        </div>
      {:else if listingLoading}
        <div class="center-msg">Loading folder…</div>
      {:else if listingError}
        <div class="center-msg error">
          <p>{listingError}</p>
          <Button variant="secondary" onclick={() => loadFolder(selected!)}>Retry</Button>
        </div>
      {:else if listing}
        <FileTable {listing} {place} {showHidden} {href} onNavigate={navigate} />
      {/if}
    </FileDropZone>
  {/if}
</AppShell>

<Modal
  bind:open={folderOpen}
  title="New folder"
  onOpenAutoFocus={(event) => {
    event.preventDefault();
    document.getElementById('workspace-folder-name')?.focus();
  }}
  description={`Create in My files${folderPath ? ` / ${folderPath}` : ''}`}
  dismissible={!folderBusy}
  width="420px"
>
  <form onsubmit={createFolder}>
    <Field label="Folder name"
      ><Input
        id="workspace-folder-name"
        bind:value={folderName}
        required
        disabled={folderBusy}
      /></Field
    >
    {#if folderError}<p class="form-error" role="alert">{folderError}</p>{/if}
    <div class="form-actions">
      <Button variant="secondary" onclick={() => (folderOpen = false)} disabled={folderBusy}
        >Cancel</Button
      >
      <Button type="submit" disabled={folderBusy || !folderName.trim()}
        >{folderBusy ? 'Creating…' : 'Create folder'}</Button
      >
    </div>
  </form>
</Modal>

<style>
  .folder-actions,
  .upload-heading {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .uploads {
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    max-height: 30vh;
    overflow-y: auto;
    flex-shrink: 0;
  }
  .upload-heading {
    justify-content: space-between;
  }
  .uploads ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .upload-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, auto);
    gap: var(--space-2);
    padding: var(--space-2) 0;
    font-size: var(--text-sm);
  }
  .upload-name {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .upload-status {
    text-align: right;
  }
  progress {
    grid-column: 1 / -1;
    width: 100%;
    height: var(--space-1);
    accent-color: var(--accent);
  }
  .form-actions {
    margin-top: var(--space-4);
  }

  .folder-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-3);
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  nav {
    min-width: 0;
  }
  ol {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  a {
    color: var(--text-muted);
    text-decoration: none;
  }
  a[aria-current='page'] {
    color: var(--text-primary);
    font-weight: 500;
  }
  a:hover {
    text-decoration: underline;
  }
  .hidden-toggle {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-muted);
    white-space: nowrap;
    cursor: pointer;
  }
  input {
    margin: 0;
  }
  @media (pointer: coarse) {
    li a,
    .hidden-toggle {
      min-height: var(--touch-min);
      display: inline-flex;
      align-items: center;
    }
  }
</style>
