<script lang="ts">
  import { ExternalLink, File, Folder, Link, Minus, Users } from '@lucide/svelte';
  import type { WorkspaceListing, WorkspacePlace } from '$lib/api';
  import { formatBytes } from '$lib/format';
  import { formatDate, formatDateTime } from '$lib/dateFormat';
  import { viewer } from '$lib/fileViewer/store.svelte';
  let {
    listing,
    place,
    showHidden,
    href,
    onNavigate,
  }: {
    listing: WorkspaceListing;
    place: WorkspacePlace;
    showHidden: boolean;
    href: (place: string, path?: string) => string;
    onNavigate: (event: MouseEvent, place: string, path?: string) => void;
  } = $props();
  let entries = $derived(listing.entries.filter((entry) => showHidden || !entry.hidden));
  function entryPath(name: string) {
    return [listing.path, name].filter(Boolean).join('/');
  }
</script>

{#if entries.length}
  <table aria-label="Files">
    <thead
      ><tr
        ><th scope="col">Name</th><th scope="col" class="size">Size</th><th
          scope="col"
          class="modified">Modified</th
        ></tr
      ></thead
    >
    <tbody>
      {#each entries as entry (entry.name)}
        {@const iso = new Date(entry.modified * 1000).toISOString()}
        <tr>
          <td>
            <div class="entry-name">
              <span class="entry-icon" aria-hidden="true">
                {#if entry.kind === 'dir'}<Folder size={16} />{:else if entry.kind === 'link'}<Link
                    size={16}
                  />{:else if entry.kind === 'file'}<File size={16} />{:else}<Minus
                    size={16}
                  />{/if}
              </span>
              <div class="entry-label">
                {#if entry.kind === 'dir'}
                  <a
                    href={href(place.id, entryPath(entry.name))}
                    onclick={(event) => onNavigate(event, place.id, entryPath(entry.name))}
                    >{entry.name}</a
                  >
                {:else if entry.kind === 'file' && place.view_root}
                  <button
                    type="button"
                    onclick={() => viewer.openFile(`${place.view_root}/${entryPath(entry.name)}`)}
                    >{entry.name}</button
                  >
                {:else}
                  <span>{entry.name}</span>
                {/if}
                {#if entry.share}
                  <span class="share-marker" title={`Nextcloud ${entry.share.kind} shares`}>
                    <Users size={12} aria-hidden="true" /> Shared{entry.share.count > 1
                      ? ` · ${entry.share.count}`
                      : ''}
                  </span>
                {/if}
                {#if entry.nextcloud_url && (entry.kind === 'file' || entry.kind === 'dir')}
                  <a
                    class="nextcloud-link"
                    href={entry.nextcloud_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`Open ${entry.name} in Nextcloud`}
                  >
                    Open in Nextcloud <ExternalLink size={12} aria-hidden="true" />
                  </a>
                {/if}
                {#if entry.kind === 'link'}<span class="caption">Symbolic link · not followed</span
                  >{:else if entry.kind === 'other'}<span class="caption">Not a regular file</span
                  >{/if}
              </div>
            </div>
          </td>
          <td class="size muted">{entry.size === null ? '—' : formatBytes(entry.size)}</td>
          <td class="modified muted"
            ><time datetime={iso} title={formatDateTime(iso)}>{formatDate(iso)}</time></td
          >
        </tr>
      {/each}
    </tbody>
  </table>
{:else}
  <div class="center-msg">
    {listing.entries.length ? 'No visible files in this folder.' : 'This folder is empty.'}
  </div>
{/if}
{#if listing.truncated || listing.skipped_names || listing.notes?.length}
  <div class="listing-notes muted" role="status">
    {#each listing.notes ?? [] as note}<p>{note}</p>{/each}
    {#if listing.truncated}<p>Showing the first 2,000 entries.</p>{/if}
    {#if listing.skipped_names}<p>
        {listing.skipped_names}
        {listing.skipped_names === 1 ? 'entry with a name' : 'entries with names'} that can't be shown.
      </p>{/if}
  </div>
{/if}

<style>
  table {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }
  th {
    text-align: left;
    font-weight: 500;
    color: var(--text-dim);
    font-size: var(--text-xs);
  }
  th,
  td {
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
  }
  .size {
    width: 6rem;
    text-align: right;
    white-space: nowrap;
  }
  .modified {
    width: 9rem;
    white-space: nowrap;
  }
  .entry-name {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-width: 0;
  }
  .entry-icon {
    display: flex;
    color: var(--text-muted);
    flex-shrink: 0;
  }
  .entry-label {
    display: flex;
    flex-direction: column;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .entry-label a,
  .entry-label button {
    color: var(--text-primary);
    text-decoration: none;
    text-align: left;
  }
  .entry-label button {
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .entry-label a:hover,
  .entry-label button:hover {
    text-decoration: underline;
  }
  .share-marker,
  .entry-label .nextcloud-link {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }
  .entry-label .nextcloud-link {
    color: var(--accent);
  }
  tbody tr:hover {
    background: var(--surface-card);
  }
  .listing-notes {
    padding: var(--space-3);
    font-size: var(--text-sm);
  }
  .listing-notes p {
    margin: 0 0 var(--space-1);
  }
  @media (max-width: 640px) {
    .modified {
      display: none;
    }
    .size {
      width: 4.5rem;
    }
    .entry-name {
      gap: var(--space-2);
    }
  }
  @media (pointer: coarse) {
    .entry-label a,
    .entry-label button {
      min-height: var(--touch-min);
      display: flex;
      align-items: center;
    }
  }
</style>
