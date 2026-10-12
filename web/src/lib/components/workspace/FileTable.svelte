<script lang="ts">
  import {
    Folder,
    Link,
    Minus,
    Users,
    ArrowUp,
    ArrowDown,
    ArrowUpDown,
    RefreshCw,
  } from '@lucide/svelte';
  import type {
    DriveItem,
    WorkspaceListing,
    WorkspacePlace,
    WorkspacePin,
    WorkspacePinInput,
  } from '$lib/api';
  import {
    Button,
    Input,
    Select,
    IconButton,
    ConfirmDialog,
    KebabMenu,
    type KebabItem,
  } from '$lib/components/ui';
  import { chatFileUrl } from '$lib/api';
  import { formatBytes } from '$lib/format';
  import { formatDate, formatDateTime } from '$lib/dateFormat';
  import { fileIcon } from './filePresentation';
  import { loadSetting, saveSetting } from '$lib/stores/persisted';
  import { viewer } from '$lib/fileViewer/store.svelte';
  let {
    listing = null,
    driveItems = [],
    drivePath = '',
    place,
    showHidden,
    href,
    onNavigate,
    pins = [],
    pinsBusy = false,
    onTogglePin,
    onDelete,
    onRefresh,
  }: {
    onDelete?: (names: string[]) => Promise<{ deleted: string[]; errors: string[] }>;
    onRefresh?: () => void;
    pins?: WorkspacePin[];
    pinsBusy?: boolean;
    onTogglePin?: (pin: WorkspacePinInput) => void;
    listing?: WorkspaceListing | null;
    driveItems?: DriveItem[];
    drivePath?: string;
    place: WorkspacePlace;
    showHidden: boolean;
    href: (place: string, path?: string) => string;
    onNavigate: (event: MouseEvent, place: string, path?: string) => void;
  } = $props();
  let allEntries = $derived(
    place.kind === 'drive'
      ? driveItems.map((item) => ({
          key: item.id,
          ref: item.id,
          mime_type: item.mime_type,
          name: item.name,
          kind: item.is_folder ? 'dir' : 'file',
          size: item.size,
          modified: item.modified,
          path: [drivePath, `${item.id}:${encodeURIComponent(item.name)}`]
            .filter(Boolean)
            .join('/'),
          web_url: item.web_url,
          share: null,
          nextcloud_url: null,
        }))
      : (listing?.entries || [])
          .filter((entry) => showHidden || !entry.hidden)
          .map((entry) => ({
            ...entry,
            key: entry.name,
            ref:
              entry.view_path ??
              (place.view_root
                ? [place.view_root, listing?.path, entry.name].filter(Boolean).join('/')
                : null),
            mime_type: null,
            modified: new Date(entry.modified * 1000).toISOString(),
            path: [listing?.path, entry.name].filter(Boolean).join('/'),
            web_url: place.kind === 'nextcloud' ? (entry.nextcloud_url ?? null) : null,
          })),
  );
  type SortKey = 'name' | 'size' | 'modified';
  const saved = loadSetting<{ key?: string; descending?: boolean } | null>('workspace.sort', null);
  let sortKey = $state<SortKey>(
    saved?.key === 'size' || saved?.key === 'modified' ? saved.key : 'name',
  );
  let descending = $state(saved?.descending === true);
  let foldersFirst = $state(loadSetting<boolean>('workspace.foldersFirst', true) !== false);
  let query = $state('');
  let selectedKeys = $state<string[]>([]);
  let deleteOpen = $state(false);
  let deleting = $state(false);
  let deleteErrors = $state<string[]>([]);
  let pendingNames = $state<string[]>([]);
  let entries = $derived.by(() => {
    const visible = allEntries.filter((entry) =>
      entry.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
    );
    return visible.sort((a, b) => {
      if (foldersFirst && (a.kind === 'dir') !== (b.kind === 'dir'))
        return a.kind === 'dir' ? -1 : 1;
      let order = 0;
      if (sortKey === 'name')
        order = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
      else {
        const av = sortKey === 'size' ? a.size : a.modified ? Date.parse(a.modified) : null;
        const bv = sortKey === 'size' ? b.size : b.modified ? Date.parse(b.modified) : null;
        if (av === null && bv !== null) return 1;
        if (bv === null && av !== null) return -1;
        order = (av ?? 0) - (bv ?? 0);
      }
      return (
        (descending ? -order : order) ||
        a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
      );
    });
  });
  let selectable = $derived(
    entries.filter((entry) => entry.kind === 'file' || entry.kind === 'dir'),
  );
  let selectedEntries = $derived(selectable.filter((entry) => selectedKeys.includes(entry.key)));
  let folderContext = $derived(`${place.id}:${listing?.path ?? drivePath}`);
  $effect(() => {
    query;
    showHidden;
    folderContext;
    selectedKeys = [];
  });
  function sort(key: SortKey) {
    descending = key === sortKey ? !descending : key !== 'name';
    sortKey = key;
    saveSetting('workspace.sort', { key: sortKey, descending });
  }
  function toggle(key: string) {
    selectedKeys = selectedKeys.includes(key)
      ? selectedKeys.filter((item) => item !== key)
      : [...selectedKeys, key];
  }
  function requestDelete(names = selectedEntries.map((entry) => entry.name)) {
    pendingNames = names;
    deleteOpen = true;
  }
  async function deleteSelected() {
    if (!onDelete || deleting || !pendingNames.length) return;
    deleteOpen = false;
    deleting = true;
    deleteErrors = [];
    try {
      const result = await onDelete([...pendingNames]);
      selectedKeys = selectedKeys.filter((key) => !result.deleted.includes(key));
      deleteErrors = result.errors;
    } catch (e) {
      deleteErrors = [e instanceof Error ? e.message : 'Could not delete the selected items.'];
    } finally {
      deleting = false;
    }
  }
  function menuItems(entry: (typeof entries)[number]): KebabItem[] {
    if (entry.kind !== 'file' && entry.kind !== 'dir') return [];
    const items: KebabItem[] = [];
    if (onTogglePin && entry.ref) {
      const provider = place.kind === 'drive' ? 'drive' : 'workspace';
      const pinned = pins.some((pin) => pin.provider === provider && pin.ref === entry.ref);
      items.push({
        label: pinned ? 'Unpin' : 'Pin',
        disabled: pinsBusy,
        onSelect: () =>
          onTogglePin!({
            provider,
            ref: entry.ref!,
            name: entry.name,
            mime_type: entry.mime_type,
            web_url: entry.web_url,
          }),
      });
    }
    if (place.kind !== 'drive' && entry.kind === 'file' && entry.ref) {
      items.push({ label: 'Download', href: chatFileUrl(entry.ref), download: entry.name });
    }
    const providerUrl = entry.nextcloud_url || entry.web_url;
    if (providerUrl) {
      items.push({
        label: entry.nextcloud_url ? 'Open in Nextcloud' : 'Open in Google Drive',
        href: providerUrl,
        target: '_blank',
      });
    }
    if (onDelete)
      items.push({
        label: 'Delete',
        disabled: deleting,
        onSelect: () => requestDelete([entry.name]),
      });
    return items;
  }
</script>

<div class="file-controls control-row">
  <div class="file-filter">
    <Input
      aria-label="Filter files"
      placeholder="Filter files…"
      bind:value={query}
      disabled={deleting}
    />
  </div>
  <label class="folders-toggle"
    ><input
      type="checkbox"
      bind:checked={foldersFirst}
      onchange={() => saveSetting('workspace.foldersFirst', foldersFirst)}
    />Folders first</label
  >
  <div class="mobile-sort">
    <Select
      ariaLabel="Sort files"
      value={sortKey}
      options={[
        { value: 'name', label: 'Name' },
        { value: 'modified', label: 'Modified' },
        { value: 'size', label: 'Size' },
      ]}
      onValueChange={(value) => sort(value as SortKey)}
    /><IconButton
      label={descending ? 'Sort ascending' : 'Sort descending'}
      onclick={() => sort(sortKey)}
      >{#if descending}<ArrowDown size={16} />{:else}<ArrowUp size={16} />{/if}</IconButton
    >
  </div>
  {#if onRefresh}<IconButton
      label="Refresh files"
      title="Refresh files"
      disabled={deleting}
      onclick={onRefresh}><RefreshCw size={16} /></IconButton
    >{/if}
</div>
<div class="selection-toolbar">
  <span class="muted" role="status"
    >{selectedEntries.length
      ? `${selectedEntries.length} selected`
      : `${entries.length} ${entries.length === 1 ? 'item' : 'items'}`}</span
  >
  {#if selectedEntries.length}<Button
      variant="ghost"
      size="sm"
      disabled={deleting}
      onclick={() => (selectedKeys = [])}>Clear selection</Button
    >{/if}
  {#if onDelete}<Button
      variant="danger"
      size="sm"
      disabled={!selectedEntries.length || deleting}
      loading={deleting}
      loadingLabel="Deleting…"
      onclick={() => requestDelete()}>Delete selected</Button
    >{/if}
</div>
{#if deleteErrors.length}<div class="delete-errors form-error" role="alert">
    {#each deleteErrors as message}<p>{message}</p>{/each}
  </div>{/if}
<ConfirmDialog
  bind:open={deleteOpen}
  title={`Delete ${pendingNames.length === 1 ? pendingNames[0] : `${pendingNames.length} items`}`}
  message="This permanently deletes the selected files and empty folders. Folders containing files cannot be deleted here. This cannot be undone."
  onConfirm={deleteSelected}
/>
{#if entries.length}
  <table aria-label="Files">
    <thead
      ><tr>
        <th scope="col" class="selection-column"
          ><input
            type="checkbox"
            aria-label="Select all visible items"
            disabled={deleting || !selectable.length}
            checked={!!selectable.length && selectedEntries.length === selectable.length}
            indeterminate={selectedEntries.length > 0 && selectedEntries.length < selectable.length}
            onchange={(event) =>
              (selectedKeys = event.currentTarget.checked
                ? selectable.map((entry) => entry.key)
                : [])}
          /></th
        >
        {#each [{ key: 'name', label: 'Name' }, { key: 'size', label: 'Size' }, { key: 'modified', label: 'Modified' }] as column}
          <th
            scope="col"
            class={column.key === 'name' ? '' : column.key}
            aria-sort={sortKey === column.key ? (descending ? 'descending' : 'ascending') : 'none'}
          >
            <button
              class="sort-button touch-target"
              aria-label={`Sort by ${column.key}`}
              onclick={() => sort(column.key as SortKey)}
              >{column.label}{#if sortKey !== column.key}<ArrowUpDown
                  size={14}
                />{:else if descending}<ArrowDown size={14} />{:else}<ArrowUp
                  size={14}
                />{/if}</button
            >
          </th>
        {/each}
        <th scope="col" class="actions-column" aria-label="Actions"></th>
      </tr></thead
    >
    <tbody>
      {#each entries as entry (entry.key)}
        {@const iso = entry.modified}
        {@const actions = menuItems(entry)}
        {@const EntryIcon = fileIcon(entry.name, entry.mime_type)}
        <tr class:selected={selectedKeys.includes(entry.key)}>
          <td class="selection-column"
            ><input
              type="checkbox"
              aria-label={`Select ${entry.name}`}
              disabled={deleting || (entry.kind !== 'file' && entry.kind !== 'dir')}
              checked={selectedKeys.includes(entry.key)}
              onchange={() => toggle(entry.key)}
            /></td
          >
          <td>
            <div class="entry-name">
              <span class="entry-icon" aria-hidden="true">
                {#if entry.kind === 'dir'}<Folder size={16} />{:else if entry.kind === 'link'}<Link
                    size={16}
                  />{:else if entry.kind === 'file'}<EntryIcon size={16} />{:else}<Minus
                    size={16}
                  />{/if}
              </span>
              <div class="entry-label" title={entry.name}>
                {#if entry.kind === 'dir'}
                  <a
                    href={href(place.id, entry.path)}
                    onclick={(event) => onNavigate(event, place.id, entry.path)}>{entry.name}</a
                  >
                {:else if entry.kind === 'file' && entry.web_url}
                  <a href={entry.web_url} target="_blank" rel="noopener noreferrer">{entry.name}</a>
                {:else if entry.kind === 'file' && place.view_root}
                  <button type="button" onclick={() => viewer.openFile(entry.ref!)}
                    >{entry.name}</button
                  >
                {:else}
                  <span>{entry.name}</span>
                {/if}
                {#if entry.share}
                  <span
                    class="share-marker"
                    role="img"
                    aria-label={`Shared · ${entry.share.count} ${entry.share.kind} shares`}
                    title={`Nextcloud ${entry.share.kind} shares`}
                  >
                    <Users size={14} aria-hidden="true" /><span class="marker-label"
                      >Shared{entry.share.count > 1 ? ` · ${entry.share.count}` : ''}</span
                    >
                  </span>
                {/if}
                {#if entry.kind === 'link'}<span class="caption marker-label"
                    >Symbolic link · not followed</span
                  >{:else if entry.kind === 'other'}<span class="caption marker-label"
                    >Not a regular file</span
                  >{/if}
              </div>
            </div>
          </td>
          <td class="size muted">{entry.size === null ? '—' : formatBytes(entry.size)}</td>
          <td class="modified muted"
            >{#if iso}<time datetime={iso} title={formatDateTime(iso)}>{formatDate(iso)}</time
              >{:else}—{/if}</td
          >
          <td class="actions-column">
            {#if actions.length}
              <KebabMenu items={actions} ariaLabel={`Actions for ${entry.name}`} />
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
{:else}
  <div class="center-msg">
    {query
      ? 'No matching files.'
      : listing?.entries.length
        ? 'No visible files in this folder.'
        : 'This folder is empty.'}
  </div>
{/if}
{#if listing && (listing.truncated || listing.skipped_names || listing.notes?.length)}
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
  .file-controls,
  .selection-toolbar,
  .folders-toggle,
  .mobile-sort {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }
  .file-controls,
  .selection-toolbar {
    padding: var(--space-2) var(--space-3);
    flex-wrap: wrap;
    font-size: var(--text-sm);
  }
  .selection-toolbar {
    min-height: calc(var(--control-height-md) + 2 * var(--space-2));
    flex-wrap: nowrap;
    gap: var(--space-2);
  }
  .selection-toolbar [role='status'] {
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .selection-toolbar :global(button) {
    flex-shrink: 0;
  }
  .file-filter {
    flex: 1;
    min-width: 10rem;
    max-width: 24rem;
  }
  .folders-toggle {
    white-space: nowrap;
    gap: var(--space-2);
    color: var(--text-muted);
  }
  .mobile-sort {
    display: none;
  }
  .selection-column {
    width: 2rem;
    text-align: center;
    padding-inline: var(--space-2);
  }
  .selection-column + th,
  .selection-column + td {
    padding-inline-start: 0;
  }
  input[type='checkbox'] {
    margin: 0;
    accent-color: var(--accent);
    cursor: pointer;
  }
  .sort-button {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 0;
    border: 0;
    background: none;
    color: inherit;
    font: inherit;
    cursor: pointer;
  }
  tr.selected {
    background: var(--surface-raised);
  }
  .delete-errors {
    padding: var(--space-2) var(--space-3);
  }
  .delete-errors p {
    margin: 0;
  }
  .entry-label > span:not(.share-marker) {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  @media (max-width: 640px) {
    .mobile-sort {
      display: flex;
    }
    .marker-label {
      display: none;
    }
    .file-controls {
      gap: var(--space-2);
    }
    th,
    td {
      padding-inline: var(--space-2);
    }
  }

  .actions-column {
    width: 3rem;
    text-align: center;
    padding-inline: var(--space-1);
  }

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
    min-height: var(--touch-min);
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-width: 0;
  }
  .entry-icon {
    display: flex;
    color: var(--accent-amber);
    flex-shrink: 0;
  }
  .entry-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
  }
  .entry-label a,
  .entry-label button {
    color: var(--text-primary);
    text-decoration: none;
    text-align: left;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
  .share-marker {
    flex-shrink: 0;
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-muted);
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
      min-height: var(--touch-min);
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
