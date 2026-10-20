<script lang="ts">
  import { Folder, Link, Minus, Users, ArrowUp, ArrowDown, RefreshCw } from '@lucide/svelte';
  import type {
    DriveItem,
    WorkspaceListing,
    WorkspacePlace,
    WorkspacePin,
    WorkspacePinInput,
  } from '$lib/api';
  import {
    Icon,
    Button,
    Input,
    Select,
    IconButton,
    ConfirmDialog,
    KebabMenu,
    EmptyState,
    SortHeader,
    sortAria,
    type KebabItem,
    Checkbox,
    Timestamp,
  } from '$lib/components/ui';
  import { chatFileUrl, nextcloudFileUrl } from '$lib/api';
  import { formatBytes } from '$lib/format';
  import { fileIcon } from './filePresentation';
  import { loadSetting, saveSetting } from '$lib/ui/persisted';
  import { createTableSort } from '$lib/ui/tableSort.svelte';
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
            web_url: null,
          })),
  );
  type SortKey = 'name' | 'size' | 'modified';
  const COLUMNS: { key: SortKey; label: string; class: string }[] = [
    { key: 'name', label: 'Name', class: '' },
    { key: 'size', label: 'Size', class: 'size num' },
    { key: 'modified', label: 'Modified', class: 'modified' },
  ];
  // A name reads A to Z first; a size or a date biggest and newest first.
  const sort = createTableSort<SortKey>('workspace.sort', ['name', 'size', 'modified'], {
    initial: { key: 'name', direction: 'ascending' },
    firstDirection: (key) => (key === 'name' ? 'ascending' : 'descending'),
  });
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
      if (sort.key === 'name')
        order = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
      else {
        const av = sort.key === 'size' ? a.size : a.modified ? Date.parse(a.modified) : null;
        const bv = sort.key === 'size' ? b.size : b.modified ? Date.parse(b.modified) : null;
        if (av === null && bv !== null) return 1;
        if (bv === null && av !== null) return -1;
        order = (av ?? 0) - (bv ?? 0);
      }
      return (
        (sort.descending ? -order : order) ||
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
    if (place.kind === 'nextcloud' && entry.kind === 'file') {
      items.push({ label: 'Download', href: nextcloudFileUrl(entry.path), download: entry.name });
    } else if (place.kind !== 'drive' && entry.kind === 'file' && entry.ref) {
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

<div class="file-controls toolbar control-row">
  <div class="file-filter">
    <Input
      aria-label="Filter files"
      placeholder="Filter files…"
      bind:value={query}
      disabled={deleting}
    />
  </div>
  <Checkbox
    class="folders-toggle"
    label="Folders first"
    tone="muted"
    bind:checked={foldersFirst}
    onchange={() => saveSetting('workspace.foldersFirst', foldersFirst)}
  />
  <div class="mobile-sort">
    <Select
      ariaLabel="Sort files"
      value={sort.key}
      options={[
        { value: 'name', label: 'Name' },
        { value: 'modified', label: 'Modified' },
        { value: 'size', label: 'Size' },
      ]}
      onValueChange={(value) => sort.toggle(value as SortKey)}
    /><IconButton
      label={sort.descending ? 'Sort ascending' : 'Sort descending'}
      onclick={() => sort.toggle(sort.key)}
      >{#if sort.descending}<Icon icon={ArrowDown} size="md" />{:else}<Icon
          icon={ArrowUp}
          size="md"
        />{/if}</IconButton
    >
  </div>
  {#if onRefresh}<IconButton
      label="Refresh files"
      title="Refresh files"
      disabled={deleting}
      onclick={onRefresh}
      icon={RefreshCw}
    />{/if}
</div>
<div class="selection-toolbar toolbar">
  <span class="muted" role="status"
    >{selectedEntries.length
      ? `${selectedEntries.length} selected`
      : `${entries.length} ${entries.length === 1 ? 'item' : 'items'}`}</span
  >
  {#if selectedEntries.length}<Button
      variant="secondary"
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
  <table class="table table--fixed table--interactive" aria-label="Files">
    <thead
      ><tr>
        <th scope="col" class="col-select"
          ><Checkbox
            ariaLabel="Select all visible items"
            disabled={deleting || !selectable.length}
            checked={!!selectable.length && selectedEntries.length === selectable.length}
            indeterminate={selectedEntries.length > 0 && selectedEntries.length < selectable.length}
            onchange={(on) => (selectedKeys = on ? selectable.map((entry) => entry.key) : [])}
          /></th
        >
        {#each COLUMNS as column (column.key)}
          <th scope="col" class={column.class} aria-sort={sortAria(sort.directionOf(column.key))}>
            <SortHeader
              label={column.label}
              class="touch-target"
              direction={sort.directionOf(column.key)}
              onclick={() => sort.toggle(column.key)}
            />
          </th>
        {/each}
        <th scope="col" class="col-actions" aria-label="Actions"></th>
      </tr></thead
    >
    <tbody>
      {#each entries as entry (entry.key)}
        {@const iso = entry.modified}
        {@const actions = menuItems(entry)}
        {@const EntryIcon = fileIcon(entry.name, entry.mime_type)}
        <tr class:is-selected={selectedKeys.includes(entry.key)}>
          <td class="col-select"
            ><Checkbox
              ariaLabel={`Select ${entry.name}`}
              disabled={deleting || (entry.kind !== 'file' && entry.kind !== 'dir')}
              checked={selectedKeys.includes(entry.key)}
              onchange={() => toggle(entry.key)}
            /></td
          >
          <td>
            <div class="entry-name">
              <span class="entry-icon" aria-hidden="true">
                {#if entry.kind === 'dir'}<Icon
                    icon={Folder}
                    size="md"
                  />{:else if entry.kind === 'link'}<Icon
                    icon={Link}
                    size="md"
                  />{:else if entry.kind === 'file'}<Icon icon={EntryIcon} size="md" />{:else}<Icon
                    icon={Minus}
                    size="md"
                  />{/if}
              </span>
              <div class="entry-label" title={entry.name}>
                {#if entry.kind === 'dir'}
                  <a
                    href={href(place.id, entry.path)}
                    onclick={(event) => onNavigate(event, place.id, entry.path)}>{entry.name}</a
                  >
                {:else if entry.kind === 'file' && place.kind === 'nextcloud'}
                  <button
                    class="button-reset"
                    type="button"
                    onclick={() =>
                      viewer.openFile(entry.path, {
                        kind: 'nextcloud',
                        webUrl: entry.nextcloud_url ?? null,
                      })}>{entry.name}</button
                  >
                {:else if entry.kind === 'file' && entry.web_url}
                  <a href={entry.web_url} target="_blank" rel="noopener noreferrer">{entry.name}</a>
                {:else if entry.kind === 'file' && place.view_root}
                  <button
                    class="button-reset"
                    type="button"
                    onclick={() => viewer.openFile(entry.ref!)}>{entry.name}</button
                  >
                {:else}
                  <span class="truncate">{entry.name}</span>
                {/if}
                {#if entry.share}
                  <span
                    class="share-marker caption"
                    role="img"
                    aria-label={`Shared · ${entry.share.count} ${entry.share.kind} shares`}
                    title={`Nextcloud ${entry.share.kind} shares`}
                  >
                    <Icon icon={Users} size="sm" /><span class="marker-label"
                      >Shared{entry.share.count > 1 ? ` · ${entry.share.count}` : ''}</span
                    >
                  </span>
                {/if}
                {#if entry.kind === 'link'}<span class="caption marker-label truncate"
                    >Symbolic link · not followed</span
                  >{:else if entry.kind === 'other'}<span class="caption marker-label truncate"
                    >Not a regular file</span
                  >{/if}
              </div>
            </div>
          </td>
          <td class="size num muted">{entry.size === null ? '—' : formatBytes(entry.size)}</td>
          <td class="modified muted"><Timestamp value={iso} empty="—" /></td>
          <td class="col-actions">
            {#if actions.length}
              <KebabMenu items={actions} ariaLabel={`Actions for ${entry.name}`} />
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
{:else}
  <EmptyState>
    {query
      ? 'No matching files.'
      : listing?.entries.length
        ? 'No visible files in this folder.'
        : 'This folder is empty.'}
  </EmptyState>
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
  .mobile-sort {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }
  /* Inset like a bar but not ruled: the folder bar above draws the rule. */
  .file-controls,
  .selection-toolbar {
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
  }
  .selection-toolbar {
    min-height: calc(var(--control-height-md) + 2 * var(--space-2));
    flex-wrap: nowrap;
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
  .file-controls :global(.folders-toggle) {
    white-space: nowrap;
  }
  .mobile-sort {
    display: none;
  }
  .col-select + th,
  .col-select + td {
    padding-inline-start: 0;
  }
  .file-controls :global(.ui-checkbox-input),
  .col-select :global(.ui-checkbox-input) {
    margin: 0;
  }
  .delete-errors {
    padding: var(--space-2) var(--space-3);
  }
  .delete-errors p {
    margin: 0;
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
  }

  .size {
    width: 6rem;
  }
  .modified {
    width: 9rem;
    white-space: nowrap;
  }
  .entry-name {
    min-height: var(--touch-min);
    display: flex;
    align-items: center;
    gap: var(--space-2);
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
