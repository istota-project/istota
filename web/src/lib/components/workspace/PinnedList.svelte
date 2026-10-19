<script lang="ts">
  import { CategoryGroup, KebabMenu, ListRow, type KebabItem } from '$lib/components/ui';
  import type { WorkspaceConfigFile, WorkspacePin, WorkspacePlace } from '$lib/api';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import TaskReachIcon from './TaskReachIcon.svelte';
  import { placeForRef, reachesTasks, type TaskReachScope } from './reach';
  let {
    pins,
    places,
    botName,
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
    botName: string;
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
  // The place whose reach a pin shows, or null where the row has no link or button to carry the description.
  function pinScope(place: WorkspacePlace | null): TaskReachScope | undefined {
    if (place?.kind === 'room') return 'room';
    if (place?.kind === 'group') return 'group';
    return undefined;
  }

  function pinPlace(pin: WorkspacePin) {
    if (!pin.available) return null;
    if (pin.provider === 'drive') {
      return pin.web_url ? (places.find((place) => place.kind === 'drive') ?? null) : null;
    }
    return placeForRef(places, pin.ref);
  }
  function folderTarget(pin: WorkspacePin) {
    if (!pin.available || !pin.is_dir || pin.provider !== 'workspace') return null;
    const place = placeForRef(places, pin.ref);
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
      {@const pinned = pinPlace(pin)}
      {@const access = pinned?.reach.private ?? null}
      {@const reachId = `task-reach-pin-${pin.id}`}
      {@const describedBy = access && reachesTasks(access) ? reachId : undefined}
      {#snippet reach()}
        {#if access}<TaskReachIcon {access} {botName} id={reachId} scope={pinScope(pinned)} />{/if}
      {/snippet}
      {#snippet unavailable()}<span class="caption">Unavailable</span>{/snippet}
      {#snippet kebab()}
        <KebabMenu ariaLabel={`Actions for ${pin.name}`} items={actions(pin)} />
      {/snippet}
      {#if pin.available && folder}
        <ListRow
          class="pin-row"
          density="sm"
          href={href(folder.place, folder.path)}
          title={pin.name}
          aria-describedby={describedBy}
          onclick={(event) => onNavigate(event, folder.place, folder.path)}
          badge={reach}
          trailing={kebab}>{pin.name}</ListRow
        >
      {:else if pin.available && pin.provider === 'drive' && pin.web_url}
        <ListRow
          class="pin-row"
          density="sm"
          href={pin.web_url}
          target="_blank"
          rel="noopener noreferrer"
          title={pin.name}
          aria-describedby={describedBy}
          badge={reach}
          trailing={kebab}>{pin.name}</ListRow
        >
      {:else if pin.available && pin.provider === 'workspace' && !pin.is_dir}
        <ListRow
          class="pin-row"
          density="sm"
          title={pin.name}
          aria-describedby={describedBy}
          onclick={() => viewer.openFile(pin.ref)}
          badge={reach}
          trailing={kebab}>{pin.name}</ListRow
        >
      {:else}
        <ListRow
          class="pin-row"
          density="sm"
          title={pin.name}
          meta={pin.available ? undefined : unavailable}
          badge={reach}
          trailing={kebab}><span class="inert">{pin.name}</span></ListRow
        >
      {/if}
    {:else}
      <p class="caption">Pin a file or folder to keep it here.</p>
    {/each}
  </CategoryGroup>
</section>

<style>
  .inert {
    color: var(--text-dim);
  }
</style>
