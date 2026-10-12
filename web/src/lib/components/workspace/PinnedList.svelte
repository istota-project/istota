<script lang="ts">
  import { Cloud, File, Folder, X } from '@lucide/svelte';
  import { CategoryGroup, IconButton } from '$lib/components/ui';
  import type { WorkspacePin, WorkspacePlace } from '$lib/api';
  import { viewer } from '$lib/fileViewer/store.svelte';
  let {
    pins,
    places,
    busy,
    onRemove,
    href,
    onNavigate,
  }: {
    pins: WorkspacePin[];
    places: WorkspacePlace[];
    busy: boolean;
    onRemove: (pin: WorkspacePin) => void;
    href: (place: string, path?: string) => string;
    onNavigate: (event: MouseEvent, place: string, path?: string) => void;
  } = $props();
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
  <CategoryGroup label="Pinned">
    {#each pins as pin (pin.id)}
      {@const folder = folderTarget(pin)}
      <div class="list-row pin-row">
        {#snippet content()}
          {#if pin.provider === 'drive'}<Cloud size={14} />{:else if pin.is_dir}<Folder
              size={14}
            />{:else}<File size={14} />{/if}
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
        <IconButton
          size="sm"
          class="remove-pin"
          label={`Remove pin ${pin.name}`}
          title="Remove pin"
          disabled={busy}
          onclick={() => onRemove(pin)}><X size={14} /></IconButton
        >
      </div>
    {:else}
      <p class="empty-pins caption">Pin a file or folder to keep it here.</p>
    {/each}
  </CategoryGroup>
</section>

<style>
  .pin-row .view-btn,
  .unavailable {
    flex: 1;
    min-width: 0;
  }
  .unavailable {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1) var(--space-2);
    color: var(--text-dim);
    font-size: var(--text-base);
  }
  .pin-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pin-label .caption {
    display: block;
  }
  .pin-row :global(svg) {
    flex-shrink: 0;
  }
  .empty-pins {
    margin: var(--space-1) var(--space-3) var(--space-3);
  }
  @media (pointer: coarse) {
    .pin-row :global(.remove-pin) {
      min-width: var(--touch-min);
      min-height: var(--touch-min);
    }
  }
</style>
