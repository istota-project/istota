<script lang="ts">
  import { CategoryGroup, KebabMenu, type KebabItem } from '$lib/components/ui';
  import type { WorkspaceConfigFile, WorkspacePin, WorkspacePlace } from '$lib/api';
  import { viewer } from '$lib/fileViewer/store.svelte';
  let {
    pins,
    places,
    busy,
    onRemove,
    configFiles,
    onEdit,
    revealAction,
    href,
    onNavigate,
  }: {
    pins: WorkspacePin[];
    places: WorkspacePlace[];
    busy: boolean;
    onRemove: (pin: WorkspacePin) => void;
    configFiles: WorkspaceConfigFile[];
    onEdit: (file: WorkspaceConfigFile) => void;
    revealAction: (
      ref: string | null | undefined,
      provider?: 'workspace' | 'drive',
      available?: boolean,
    ) => KebabItem;
    href: (place: string, path?: string) => string;
    onNavigate: (event: MouseEvent, place: string, path?: string) => void;
  } = $props();
  function actions(pin: WorkspacePin): KebabItem[] {
    const items: KebabItem[] = [];
    if (pin.provider === 'workspace' && pin.available && !pin.is_dir) {
      const file = configFiles.find((file) => file.editable && file.view_path === pin.ref);
      if (file) items.push({ label: 'Edit', onSelect: () => onEdit(file) });
    }
    items.push(revealAction(pin.ref, pin.provider, pin.available));
    items.push({ label: 'Remove pin', disabled: busy, onSelect: () => onRemove(pin) });
    return items;
  }
  function folderTarget(pin: WorkspacePin) {
    if (!pin.available || !pin.is_dir || pin.provider !== 'workspace') return null;
    const place = places
      .filter(
        (place) =>
          place.view_root &&
          (pin.ref === place.view_root || pin.ref.startsWith(place.view_root + '/')),
      )
      .sort((a, b) => b.view_root!.length - a.view_root!.length)[0];
    return place
      ? { place: place.id, path: pin.ref.slice(place.view_root!.length).replace(/^\//, '') }
      : null;
  }
</script>

<section aria-label="Pinned files">
  <CategoryGroup
    label="Pinned"
    collapsible
    defaultOpen={false}
    storageKey="workspace:section:pinned"
  >
    {#each pins as pin (pin.id)}
      {@const folder = folderTarget(pin)}
      <div class="list-row pin-row">
        {#snippet content()}
          <span class="pin-label" title={pin.name}
            >{pin.name}{#if !pin.available}<span class="caption">Unavailable</span>{/if}</span
          >
        {/snippet}
        {#if !pin.available}
          <div class="unavailable">{@render content()}</div>
        {:else if folder}
          <a
            class="view-btn"
            href={href(folder.place, folder.path)}
            onclick={(event) => onNavigate(event, folder.place, folder.path)}>{@render content()}</a
          >
        {:else if pin.provider === 'drive' && pin.web_url}
          <a class="view-btn" href={pin.web_url} target="_blank" rel="noopener noreferrer"
            >{@render content()}</a
          >
        {:else if pin.provider === 'workspace' && !pin.is_dir}
          <button class="view-btn" type="button" onclick={() => viewer.openFile(pin.ref)}
            >{@render content()}</button
          >
        {:else}
          <div class="unavailable">{@render content()}</div>
        {/if}
        <KebabMenu ariaLabel={`Actions for ${pin.name}`} items={actions(pin)} />
      </div>
    {:else}
      <p class="caption">Pin a file or folder to keep it here.</p>
    {/each}
  </CategoryGroup>
</section>

<style>
  .pin-row .view-btn,
  .unavailable {
    flex: 1;
    min-width: 0;
    padding: var(--space-1) var(--space-3);
    font-size: var(--text-sm);
    color: inherit;
  }
  .unavailable {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-dim);
  }
  .pin-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pin-label .caption {
    display: block;
  }
</style>
