<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import {
    createPlace,
    deletePlace,
    dismissCluster,
    updatePlace,
    getPlaceStats,
    type DiscoveredCluster,
    type Place,
    type PlaceStats,
  } from '$lib/api';
  import {
    locationPlaces,
    reloadPlaces,
    mapFlyTo,
    selectedPlaceId as selectedPlaceIdStore,
    onPlaceMove as onPlaceMoveStore,
    pickingPlace as pickingPlaceStore,
    requestNewPlace as requestNewPlaceStore,
    bumpDiscoverDirty,
  } from '$lib/stores/location';
  import PlaceForm from '$lib/components/location/PlaceForm.svelte';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    CategoryGroup,
    ListRow,
    HeaderNav,
    IconButton,
    Chip,
    EmptyState,
    Timestamp,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { Cog, Pencil, X } from '@lucide/svelte';
  import { formatMinutes } from '$lib/dateFormat';

  let { children } = $props();

  let places = $derived($locationPlaces);
  let sidebarOpen = $state(false);
  let selectedPlace: Place | null = $state(null);
  let placeStats: PlaceStats | null = $state(null);
  let statsLoading = $state(false);
  let editingPlace: Place | null = $state(null);

  let picking = $state(false);
  let creating: { lat: number; lon: number; cluster?: DiscoveredCluster } | null = $state(null);
  let createError = $state('');

  $effect(() => {
    pickingPlaceStore.set(picking);
  });

  $effect(() => {
    requestNewPlaceStore.set((args) => {
      picking = false;
      creating = args;
    });
    return () => requestNewPlaceStore.set(undefined);
  });

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${base}${path}`);
  }

  function isExactActive(path: string): boolean {
    const current = page.url.pathname;
    return current === `${base}${path}` || current === `${base}${path}/`;
  }

  const navItems = $derived([
    { href: `${base}/location`, label: 'Today', active: isExactActive('/location') },
    { href: `${base}/location/history`, label: 'History', active: isActive('/location/history') },
  ]);

  const onSettings = $derived(page.url.pathname.startsWith(`${base}/location/settings`));

  function toggleSettings() {
    if (onSettings) goto(`${base}/location`);
    else goto(`${base}/location/settings`);
  }

  function startPicking() {
    if (creating) return;
    picking = !picking;
    if (picking) sidebarOpen = false;
  }

  function closeCreate() {
    creating = null;
    createError = '';
  }

  async function handleCreate(data: {
    name: string;
    lat: number;
    lon: number;
    radius_meters: number;
    category: string;
    notes: string;
  }) {
    await createPlace(data);
    closeCreate();
    await reloadPlaces();
    bumpDiscoverDirty();
  }

  async function handleDismissCluster(data: { lat: number; lon: number; radius_meters: number }) {
    try {
      await dismissCluster(data);
      closeCreate();
      bumpDiscoverDirty();
    } catch (e) {
      createError = e instanceof Error ? e.message : 'Failed to dismiss';
    }
  }

  async function handlePlaceClick(place: Place) {
    const fly = $mapFlyTo;
    if (fly) fly(place.lat, place.lon, 15);

    if (selectedPlace?.id === place.id) {
      selectedPlace = null;
      placeStats = null;
      return;
    }

    selectedPlace = place;
    placeStats = null;
    statsLoading = true;
    try {
      placeStats = await getPlaceStats(place.id);
    } catch {
      placeStats = null;
    } finally {
      statsLoading = false;
    }
  }

  function handleEditPlace(place: Place) {
    editingPlace = place;
  }

  async function handleEditSave(data: {
    name: string;
    lat: number;
    lon: number;
    radius_meters: number;
    category: string;
    notes: string;
  }) {
    if (!editingPlace) return;
    await updatePlace(editingPlace.id, data);
    editingPlace = null;
    selectedPlace = null;
    placeStats = null;
    await reloadPlaces();
  }

  async function handleDeletePlace(place: Place) {
    try {
      await deletePlace(place.id);
      if (selectedPlace?.id === place.id) {
        selectedPlace = null;
        placeStats = null;
      }
      if (editingPlace?.id === place.id) {
        editingPlace = null;
      }
      await reloadPlaces();
    } catch {
      // ignore
    }
  }

  let groupedPlaces = $derived.by(() => {
    const groups: Record<string, Place[]> = {};
    for (const p of places) {
      const cat = p.category || 'other';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(p);
    }
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  });

  const formatDuration = (minutes: number | null) => formatMinutes(minutes, '—');

  async function handlePlaceMove(placeId: number, lat: number, lon: number) {
    try {
      await updatePlace(placeId, { lat, lon });
      await reloadPlaces();
      if (selectedPlace?.id === placeId) {
        placeStats = null;
        statsLoading = true;
        try {
          placeStats = await getPlaceStats(placeId);
        } catch {
          placeStats = null;
        } finally {
          statsLoading = false;
        }
      }
    } catch {
      await reloadPlaces();
    }
  }

  $effect(() => {
    selectedPlaceIdStore.set(selectedPlace?.id ?? null);
  });

  $effect(() => {
    onPlaceMoveStore.set(handlePlaceMove);
    return () => onPlaceMoveStore.set(undefined);
  });

  function handleVisibility() {
    if (document.visibilityState === 'visible') {
      reloadPlaces().catch(() => {});
    }
  }

  onMount(() => {
    reloadPlaces().catch(() => {});
    document.addEventListener('visibilitychange', handleVisibility);
    return () => document.removeEventListener('visibilitychange', handleVisibility);
  });
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title="Location"
      onTitleClick={onSettings ? undefined : () => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open places"
    >
      {#snippet leading()}
        {#if !onSettings}
          <SidebarToggle
            open={sidebarOpen}
            label="Places"
            count={places.length}
            onclick={() => (sidebarOpen = !sidebarOpen)}
          />
        {/if}
      {/snippet}
      {#snippet nav()}
        <HeaderNav items={navItems} ariaLabel="Location section" />
      {/snippet}
      {#snippet tools()}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Location settings">
          <Cog size={14} />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    {#if !onSettings}
      <Sidebar
        title="Places"
        count={places.length}
        open={sidebarOpen}
        onClose={() => (sidebarOpen = false)}
      >
        {#snippet extras()}
          <div class="sidebar-actions">
            <button
              class="new-place-btn"
              class:active={picking}
              onclick={startPicking}
              type="button"
              title={picking ? 'Click anywhere on the map to set the location' : 'Add a new place'}
            >
              {picking ? 'Click on map…' : '+ New place'}
            </button>
            {#if createError}
              <div class="action-error">{createError}</div>
            {/if}
          </div>

          {#if selectedPlace && (statsLoading || placeStats)}
            <div class="stats-panel">
              <div class="stats-header">
                <span class="stats-name title-card">{selectedPlace.name}</span>
                <div class="stats-actions">
                  <IconButton
                    class="stats-edit touch-target"
                    size="sm"
                    label="Edit place"
                    onclick={() => handleEditPlace(selectedPlace!)}
                    title="Edit place"
                  >
                    <Pencil size={13} />
                  </IconButton>
                  <IconButton
                    class="stats-close touch-target"
                    size="sm"
                    label="Close place stats"
                    onclick={() => {
                      selectedPlace = null;
                      placeStats = null;
                    }}
                  >
                    <X size={13} />
                  </IconButton>
                </div>
              </div>
              {#if selectedPlace.notes}
                <div class="stats-notes">{selectedPlace.notes}</div>
              {/if}
              {#if statsLoading}
                <div class="stats-loading">Loading...</div>
              {:else if placeStats && placeStats.total_visits > 0}
                <div class="stats-grid">
                  <div class="stat">
                    <span class="stat-value num">{placeStats.total_visits}</span>
                    <span class="stat-label caption"
                      >{placeStats.total_visits === 1 ? 'visit' : 'visits'}</span
                    >
                  </div>
                  <div class="stat">
                    <span class="stat-value num">{formatDuration(placeStats.avg_duration_min)}</span
                    >
                    <span class="stat-label caption">avg</span>
                  </div>
                  <div class="stat">
                    <span class="stat-value num"
                      >{formatDuration(placeStats.longest_visit_min)}</span
                    >
                    <span class="stat-label caption">longest</span>
                  </div>
                  <div class="stat">
                    <span class="stat-value num"
                      >{formatDuration(placeStats.total_duration_min)}</span
                    >
                    <span class="stat-label caption">total</span>
                  </div>
                </div>
                <div class="stats-dates caption">
                  <span>First: <Timestamp value={placeStats.first_visit} empty="—" /></span>
                  <span>Last: <Timestamp value={placeStats.last_visit} empty="—" /></span>
                </div>
              {:else}
                <EmptyState size="sm">No visits recorded</EmptyState>
              {/if}
            </div>
          {/if}
        {/snippet}

        {#each groupedPlaces as [category, catPlaces] (category)}
          <CategoryGroup label={category} count={catPlaces.length} collapsible>
            {#each catPlaces as place (place.id)}
              <ListRow
                density="sm"
                active={selectedPlace?.id === place.id}
                onclick={() => handlePlaceClick(place)}
              >
                {place.name}
              </ListRow>
            {/each}
          </CategoryGroup>
        {/each}
      </Sidebar>
    {/if}
  {/snippet}

  {@render children()}
</AppShell>

{#if editingPlace}
  <PlaceForm
    place={editingPlace}
    onSave={handleEditSave}
    onDelete={handleDeletePlace}
    onCancel={() => (editingPlace = null)}
  />
{/if}

{#if creating}
  <PlaceForm
    cluster={creating.cluster}
    initialLat={creating.lat}
    initialLon={creating.lon}
    onSave={handleCreate}
    onCancel={closeCreate}
    onDismiss={creating.cluster ? handleDismissCluster : undefined}
  />
{/if}

<style>
  .sidebar-actions {
    padding: 0 var(--space-3) var(--space-2);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .new-place-btn {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-pill);
    cursor: pointer;
    text-align: center;
    transition:
      color var(--transition-fast),
      border-color var(--transition-fast);
  }

  .new-place-btn:hover {
    color: var(--text-primary);
    border-color: var(--border-hover);
  }

  .new-place-btn.active {
    color: var(--accent-amber);
    border-color: var(--accent-amber);
  }

  .action-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }

  .stats-panel {
    border-bottom: 1px solid var(--border-subtle);
    padding: var(--space-2) var(--space-3);
    flex-shrink: 0;
  }

  .stats-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: var(--space-2);
  }

  .stats-actions {
    display: flex;
    gap: 0.15rem;
  }

  @media (pointer: coarse) {
    .stats-actions {
      gap: var(--space-2);
    }
  }

  .stats-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-2) var(--space-3);
    margin-bottom: var(--space-2);
  }

  .stat {
    display: flex;
    flex-direction: column;
  }

  .stat-value {
    font-size: var(--text-sm);
    font-weight: var(--weight-medium);
    color: var(--text-primary);
  }

  .stats-dates {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }

  .stats-loading,
  .stats-notes {
    font-size: var(--text-xs);
    color: var(--text-muted);
    font-style: italic;
    white-space: pre-wrap;
    padding: var(--space-1) 0;
    border-top: 1px solid var(--border-subtle);
  }
</style>
