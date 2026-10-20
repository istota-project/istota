<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { Folder } from '@lucide/svelte';
  import { Icon, Button, EmptyState, Card, Timestamp } from '$lib/components/ui';
  import { getWorkspaceListing, getWorkspacePlaces, type WorkspaceEntry } from '$lib/api';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import { fileIcon } from '$lib/components/workspace/filePresentation';

  const RECENT = 6;
  let { groupId }: { groupId: string } = $props();
  const place = $derived(`group:${groupId}`);
  const filesHref = $derived(`${base}/files?${new URLSearchParams({ place })}`);
  let entries = $state<WorkspaceEntry[]>([]);
  let viewRoot = $state<string | null>(null);
  let loading = $state(true);
  let error = $state('');
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

  onMount(() => {
    Promise.all([getWorkspaceListing(place), getWorkspacePlaces()])
      .then(([listing, places]) => {
        if (!active) return;
        viewRoot = places.places.find((p) => p.id === place)?.view_root ?? null;
        entries = listing.entries
          .filter((entry) => !entry.hidden && (entry.kind === 'file' || entry.kind === 'dir'))
          .sort((a, b) => b.modified - a.modified)
          .slice(0, RECENT);
      })
      .catch((e) => {
        if (active) error = e instanceof Error ? e.message : 'Could not load shared files.';
      })
      .finally(() => {
        if (active) loading = false;
      });
    return () => {
      active = false;
    };
  });
</script>

<Card title="Shared files" description="Recently changed in the group folder.">
  {#snippet actions()}<Button size="sm" href={filesHref}>Open in Files</Button>{/snippet}
  {#if loading}<p class="caption">Loading files…</p>
  {:else if error}<p class="banner error">{error}</p>
  {:else if entries.length}
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
  {:else}<EmptyState size="sm">The group folder is empty.</EmptyState>{/if}
</Card>

<style>
  .files li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
  .files li:last-child {
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
  button.name:hover {
    text-decoration: underline;
  }
</style>
