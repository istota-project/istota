<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { File, Folder } from '@lucide/svelte';
  import { Button, EmptyState } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { getWorkspaceListing, getWorkspacePlaces, type WorkspaceEntry } from '$lib/api';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import { formatDateTime, formatRelative } from '$lib/dateFormat';

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

<SettingsCard title="Shared files" description="Recently changed in the group folder.">
  {#snippet actions()}<Button size="sm" href={filesHref}>Open in Files</Button>{/snippet}
  {#if loading}<p class="caption">Loading files…</p>
  {:else if error}<p class="banner error">{error}</p>
  {:else if entries.length}
    <ul class="files prose">
      {#each entries as entry (entry.name)}
        {@const target = entry.kind === 'file' ? ref(entry) : null}
        <li>
          {#if entry.kind === 'dir'}<Folder aria-hidden="true" />{:else}<File
              aria-hidden="true"
            />{/if}
          {#if entry.kind === 'dir'}<a class="name" href={folderHref(entry)}>{entry.name}</a>
          {:else if target}<button
              type="button"
              class="name link-button"
              onclick={() => viewer.openFile(target)}>{entry.name}</button
            >
          {:else}<span class="name">{entry.name}</span>{/if}
          <span class="caption" title={formatDateTime(iso(entry))}
            >{formatRelative(iso(entry))}</span
          >
        </li>
      {/each}
    </ul>
  {:else}<EmptyState size="sm">The group folder is empty.</EmptyState>{/if}
</SettingsCard>

<style>
  .files {
    list-style: none;
    margin: 0;
    padding: 0;
  }
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
  .files :global(svg) {
    flex: none;
    width: 1.1em;
    height: 1.1em;
    color: var(--accent-amber);
  }
  .name {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
    text-align: left;
  }
  .link-button {
    font: inherit;
    line-height: inherit;
    color: var(--link);
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
  }
  .link-button:hover {
    text-decoration: underline;
  }
</style>
