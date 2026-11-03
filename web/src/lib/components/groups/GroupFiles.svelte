<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { Folder } from '@lucide/svelte';
  import { Icon, Button, EmptyState, Card, LoadState, Timestamp } from '@minot/ui';
  import { notifyError } from '@minot/ui/stores/notices';
  import {
    getWorkspaceListing,
    getWorkspacePlaces,
    uploadWorkspaceFile,
    type GroupFileEvent,
    type WorkspaceEntry,
  } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import { fileIcon } from '$lib/components/workspace/filePresentation';

  const RECENT = 6;
  let {
    groupId,
    activity = [],
    onUploaded,
  }: {
    groupId: string;
    /** The group detail's `file_activity`, newest first. */
    activity?: GroupFileEvent[];
    onUploaded?: () => void;
  } = $props();
  const current = getCurrentUser();
  const place = $derived(`group:${groupId}`);
  const filesHref = $derived(`${base}/files?${new URLSearchParams({ place })}`);
  const changes = $derived(activity.slice(0, RECENT));
  let entries = $state<WorkspaceEntry[]>([]);
  let viewRoot = $state<string | null>(null);
  let writable = $state(false);
  let loading = $state(true);
  let uploading = $state(false);
  let error = $state('');
  let fileInput = $state<HTMLInputElement>();
  let active = true;

  function iso(entry: WorkspaceEntry) {
    return new Date(entry.modified * 1000).toISOString();
  }
  function ref(entry: WorkspaceEntry) {
    return entry.view_path ?? (viewRoot ? `${viewRoot}/${entry.name}` : null);
  }
  function folderHref(entry: WorkspaceEntry) {
    return `${base}/files?${new URLSearchParams({ place, path: entry.name })}`;
  }
  function describe(event: GroupFileEvent): string {
    const path = event.path;
    switch (event.event_type) {
      case 'file_uploaded':
        return path ? `uploaded ${path}` : 'uploaded a file';
      case 'folder_created':
        return path ? `created folder ${path}` : 'created a folder';
      case 'file_edited':
        return path ? `edited ${path}` : 'edited a file';
      default:
        return path ? `deleted ${path}` : 'deleted an entry';
    }
  }

  async function load() {
    try {
      const [listing, places] = await Promise.all([
        getWorkspaceListing(place),
        getWorkspacePlaces(),
      ]);
      if (!active) return;
      const found = places.places.find((p) => p.id === place);
      viewRoot = found?.view_root ?? null;
      writable = found?.writable === true;
      error = '';
      entries = listing.entries
        .filter((entry) => !entry.hidden && (entry.kind === 'file' || entry.kind === 'dir'))
        .sort((a, b) => b.modified - a.modified)
        .slice(0, RECENT);
    } catch (e) {
      if (active) error = e instanceof Error ? e.message : 'Could not load shared files.';
    } finally {
      if (active) loading = false;
    }
  }

  async function upload(files: File[]) {
    if (!files.length || uploading) return;
    uploading = true;
    let landed = 0;
    try {
      for (const file of files) {
        try {
          await uploadWorkspaceFile(place, '', file, () => {});
          landed += 1;
        } catch (e) {
          const reason = e instanceof Error ? e.message : 'The upload failed.';
          notifyError(`Could not upload ${file.name}: ${reason}`);
        }
      }
    } finally {
      uploading = false;
    }
    if (!landed || !active) return;
    await load();
    onUploaded?.();
  }

  onMount(() => {
    load();
    return () => {
      active = false;
    };
  });
</script>

<Card title="Shared files" description="Recently changed in the group folder.">
  {#snippet actions()}
    {#if writable}
      <Button
        variant="secondary"
        size="sm"
        loading={uploading}
        loadingLabel="Uploading…"
        onclick={() => fileInput?.click()}>Upload</Button
      >
      <input
        bind:this={fileInput}
        type="file"
        multiple
        hidden
        aria-label="Upload files"
        onchange={(event) => {
          upload(Array.from(event.currentTarget.files || []));
          event.currentTarget.value = '';
        }}
      />
    {/if}
    <Button size="sm" href={filesHref}>Open in Files</Button>
  {/snippet}
  <LoadState scope="card" {loading} loadingLabel="Loading files…" {error} empty={!entries.length}>
    {#snippet emptyState()}<EmptyState size="sm">The group folder is empty.</EmptyState>{/snippet}
    <ul class="files list-reset">
      {#each entries as entry (entry.name)}
        {@const target = entry.kind === 'file' ? ref(entry) : null}
        {@const EntryIcon = fileIcon(entry.name)}
        <li>
          <span class="entry-icon" aria-hidden="true"
            >{#if entry.kind === 'dir'}<Icon icon={Folder} size="md" />{:else}<Icon
                icon={EntryIcon}
                size="md"
              />{/if}</span
          >
          {#if entry.kind === 'dir'}<a
              class="name link-quiet"
              href={folderHref(entry)}
              title={entry.name}>{entry.name}</a
            >
          {:else if target}<button
              type="button"
              class="name button-reset"
              title={entry.name}
              onclick={() => viewer.openFile(target)}>{entry.name}</button
            >
          {:else}<span class="name" title={entry.name}>{entry.name}</span>{/if}
          <Timestamp class="caption" value={iso(entry)} format="relative" />
        </li>
      {/each}
    </ul>
  </LoadState>
  {#if changes.length}
    <h3 id="group-file-changes-{groupId}">Recent changes</h3>
    <ul class="changes list-reset" aria-labelledby="group-file-changes-{groupId}">
      {#each changes as event (event.id)}
        <li>
          <span class="change"><strong>{event.display_name}</strong> {describe(event)}</span>
          <Timestamp class="caption" value={event.created_at} format="relative" />
        </li>
      {/each}
    </ul>
    <p class="caption">
      Changes made by {current.user.bot_name} or directly on the server are not listed.
    </p>
  {/if}
</Card>

<style>
  .files li,
  .changes li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
  .files li:last-child,
  .changes li:last-child {
    border-bottom: none;
  }
  /* The Files table's name cell: an amber icon, the name in the body colour. */
  .entry-icon {
    display: flex;
    flex-shrink: 0;
    color: var(--accent-amber);
  }
  .name {
    flex: 1;
    min-width: 0;
    color: var(--text-primary);
    text-decoration: none;
    text-align: left;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .change {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .change strong {
    font-weight: var(--weight-medium);
  }
  button.name:hover {
    text-decoration: underline;
  }
</style>
