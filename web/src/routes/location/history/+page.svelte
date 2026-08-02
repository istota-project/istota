<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { onMount, onDestroy, tick } from 'svelte';
  import {
    getLocationPings,
    getDaySummary,
    discoverPlaces,
    listDismissedClusters,
    restoreDismissedCluster,
    type LocationPing,
    type DaySummary,
    type DaySummaryStop,
    type DiscoveredCluster,
    type DismissedCluster,
  } from '$lib/api';
  import { segmentTrips, type Trip } from '$lib/location-path';
  import {
    locationPlaces,
    mapFlyTo,
    selectedPlaceId,
    onPlaceMove,
    pickingPlace,
    requestNewPlace,
    discoverDirty,
  } from '$lib/stores/location';
  import LocationMap from '$lib/components/location/LocationMap.svelte';
  import StopsPanel from '$lib/components/location/StopsPanel.svelte';
  import { Chip, ConfirmDialog, DateRangeFilter, Select } from '$lib/components/ui';
  import { loadSetting, saveSetting } from '$lib/stores/persisted';

  let pings: LocationPing[] = $state([]);
  let summary: DaySummary | null = $state(null);
  let loading = $state(false);
  let error = $state('');
  let mapComponent: LocationMap | undefined = $state();

  let startStr = $state('');
  let endStr = $state('');
  let showHeat = $state(loadSetting('location.showHeat', false));
  let panelOpen = $state(false);
  let showDiscover = $state(loadSetting('location.showDiscover', false));
  let clusters: DiscoveredCluster[] = $state([]);
  let dismissed: DismissedCluster[] = $state([]);
  let discoverLoading = $state(false);

  $effect(() => {
    saveSetting('location.showHeat', showHeat);
  });
  $effect(() => {
    saveSetting('location.showDiscover', showDiscover);
  });

  async function loadDiscover() {
    discoverLoading = true;
    try {
      const [d, dm] = await Promise.all([discoverPlaces(), listDismissedClusters()]);
      clusters = d.clusters;
      dismissed = dm.dismissed;
    } catch {
      clusters = [];
      dismissed = [];
    } finally {
      discoverLoading = false;
    }
  }

  $effect(() => {
    if (showDiscover) {
      $discoverDirty;
      loadDiscover();
    } else {
      clusters = [];
      dismissed = [];
    }
  });

  function handleClusterClick(cluster: DiscoveredCluster) {
    $requestNewPlace?.({ lat: cluster.lat, lon: cluster.lon, cluster });
  }

  let restoreTarget: DismissedCluster | null = $state(null);

  function handleDismissedClick(d: DismissedCluster) {
    restoreTarget = d;
  }

  async function performRestore() {
    if (!restoreTarget) return;
    const id = restoreTarget.id;
    restoreTarget = null;
    try {
      await restoreDismissedCluster(id);
      await loadDiscover();
    } catch {
      // ignore
    }
  }

  let places = $derived($locationPlaces);

  function localDate(d: Date = new Date()): string {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  const today = localDate();
  let isSingleDay = $derived(startStr === endStr);

  // Trips are derived from the same filtered-ping pipeline the map draws, so
  // each trip is one continuous line between stops.
  // Only meaningful for a single day; multi-day spans aren't itemised.
  let trips = $derived<Trip[]>(isSingleDay ? segmentTrips(pings) : []);

  function yesterday(): string {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    return localDate(d);
  }

  function thisWeekStart(): string {
    const d = new Date();
    d.setDate(d.getDate() - d.getDay());
    return localDate(d);
  }

  function thisMonthStart(): string {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-01`;
  }

  function thisYearStart(): string {
    return `${new Date().getFullYear()}-01-01`;
  }

  // Ranges are lazy so the derived match below re-resolves them rather than
  // comparing against dates frozen at module load.
  const RANGE_PRESETS: { value: string; label: string; range: () => [string, string] }[] = [
    { value: 'today', label: 'Today', range: () => [today, today] },
    { value: 'yesterday', label: 'Yesterday', range: () => [yesterday(), yesterday()] },
    { value: 'week', label: 'This week', range: () => [thisWeekStart(), today] },
    { value: 'month', label: 'This month', range: () => [thisMonthStart(), today] },
    { value: 'ytd', label: 'Year to date', range: () => [thisYearStart(), today] },
  ];

  let activePreset = $derived(
    RANGE_PRESETS.find((p) => {
      const [s, e] = p.range();
      return startStr === s && endStr === e;
    })?.value ?? 'custom',
  );

  // "Custom range" is only ever a label for a hand-picked From/To — it is
  // appended so the trigger has something to show, never offered as a choice.
  let rangeOptions = $derived(
    activePreset === 'custom'
      ? [
          ...RANGE_PRESETS.map((p) => ({ value: p.value, label: p.label })),
          { value: 'custom', label: 'Custom range' },
        ]
      : RANGE_PRESETS.map((p) => ({ value: p.value, label: p.label })),
  );

  function handlePresetChange(value: string) {
    const preset = RANGE_PRESETS.find((p) => p.value === value);
    if (!preset) return;
    const [s, e] = preset.range();
    selectRange(s, e);
  }

  function readUrlParams() {
    const params = page.url.searchParams;
    const s = params.get('start') || params.get('date');
    const e = params.get('end') || params.get('date');
    startStr = s || today;
    endStr = e || today;
  }

  function updateUrl() {
    const params = new URLSearchParams();
    if (startStr) params.set('start', startStr);
    if (endStr) params.set('end', endStr);
    goto(`${base}/location/history?${params.toString()}`, { replaceState: true, noScroll: true });
  }

  async function loadData() {
    loading = true;
    error = '';
    pings = [];
    summary = null;

    try {
      if (!startStr || !endStr) return;
      if (isSingleDay) {
        const [p, s] = await Promise.all([
          getLocationPings({ date: startStr }),
          getDaySummary(startStr),
        ]);
        pings = p.pings;
        summary = s;
        panelOpen = s.stops.length > 0 || trips.length > 0;
      } else {
        const p = await getLocationPings({ start: startStr, end: endStr, limit: '50000' });
        pings = p.pings;
      }
    } catch {
      error = 'Failed to load location data';
    } finally {
      loading = false;
    }
    // Re-frame the map for the new range (the map's own fit is one-shot, so
    // a range change from the Manhattan routine to the Tokyo trip wouldn't
    // otherwise refit). tick() lets the new pings reach the map first.
    await tick();
    mapComponent?.refit();
  }

  function selectRange(start: string, end: string) {
    startStr = start;
    endStr = end;
    showHeat = false;
    updateUrl();
    loadData();
  }

  function handleRangeInput() {
    if (startStr && endStr) {
      updateUrl();
      loadData();
    }
  }

  function handleStopClick(stop: DaySummaryStop) {
    mapComponent?.flyTo(stop.lat, stop.lon);
  }

  function handleTripClick(trip: Trip) {
    mapComponent?.flyTo(
      (trip.start_lat + trip.end_lat) / 2,
      (trip.start_lon + trip.end_lon) / 2,
      13,
    );
  }

  onMount(() => {
    readUrlParams();
    loadData();
  });

  onDestroy(() => {
    mapFlyTo.set(undefined);
  });

  $effect(() => {
    if (mapComponent) {
      mapFlyTo.set((lat, lon, zoom) => mapComponent?.flyTo(lat, lon, zoom));
    }
  });
</script>

<div class="page-fill">
  <div class="controls-bar">
    <Select
      value={activePreset}
      options={rangeOptions}
      onValueChange={handlePresetChange}
      ariaLabel="Date range"
    />
    <DateRangeFilter
      bind:from={startStr}
      bind:to={endStr}
      onChange={handleRangeInput}
      labelled
      max={today}
    />
    {#if !isSingleDay && pings.length > 0}
      <Chip checked={showHeat} onclick={() => (showHeat = !showHeat)}>Heat map</Chip>
    {/if}
    <Chip checked={showDiscover} onclick={() => (showDiscover = !showDiscover)}>
      Discover
      {#if showDiscover && clusters.length > 0}
        <span class="chip-count">{clusters.length}</span>
      {/if}
    </Chip>
  </div>

  {#if loading}
    <div class="center-msg">Loading…</div>
  {:else if error}
    <div class="center-msg error">{error}</div>
  {:else if pings.length === 0 && !showDiscover}
    <div class="center-msg">No location data for this period</div>
  {:else}
    <div class="map-area">
      <LocationMap
        bind:this={mapComponent}
        {pings}
        {places}
        clusters={showDiscover ? clusters : []}
        dismissedClusters={showDiscover ? dismissed : []}
        showPath={!showHeat}
        {showHeat}
        selectedPlaceId={$selectedPlaceId}
        onPlaceMove={$onPlaceMove}
        pickingLocation={$pickingPlace}
        onMapClick={(lat, lon) => $requestNewPlace?.({ lat, lon })}
        onClusterClick={handleClusterClick}
        onDismissedClusterClick={handleDismissedClick}
      />
    </div>
    {#if pings.length === 0 && showDiscover}
      <div class="discover-hint">
        {#if discoverLoading}
          Loading discovered places…
        {:else if clusters.length === 0}
          No unknown places to discover. {dismissed.length > 0
            ? `${dismissed.length} dismissed area${dismissed.length === 1 ? '' : 's'} shown.`
            : ''}
        {:else}
          Click a yellow circle to name it.
        {/if}
      </div>
    {/if}

    <div class="stats-bar">
      {#if pings.length > 0}
        <span class="stat">{pings.length} pings</span>
      {/if}
      {#if summary && pings.length > 0}
        <span class="stat">{summary.stops.length} stops</span>
        <span class="stat">{summary.transit_pings} transit</span>
      {/if}
      {#if showDiscover && clusters.length > 0}
        <span class="stat">{clusters.length} unknown</span>
      {/if}
      {#if !isSingleDay}
        {@const uniquePlaces = new Set(pings.filter((p) => p.place).map((p) => p.place))}
        <span class="stat">{uniquePlaces.size} places</span>
      {/if}
      {#if isSingleDay && trips.length > 0}
        <span class="stat">{trips.length} trips</span>
      {/if}
      {#if (summary && summary.stops.length > 0) || trips.length > 0}
        <button class="stops-btn" onclick={() => (panelOpen = !panelOpen)} type="button">
          {panelOpen ? 'Hide details' : 'Show details'}
        </button>
      {/if}
    </div>

    {#if panelOpen && isSingleDay}
      <StopsPanel
        {pings}
        {trips}
        {summary}
        onStopClick={handleStopClick}
        onTripClick={handleTripClick}
      />
    {/if}
  {/if}
</div>

<ConfirmDialog
  open={restoreTarget != null}
  title="Restore dismissed area"
  message="Are you sure you want to restore this dismissed area? Future pings here may form a cluster again."
  confirmLabel="Restore"
  confirmVariant="primary"
  onConfirm={performRestore}
  onCancel={() => (restoreTarget = null)}
/>

<style>
  .page-fill {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .controls-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .map-area {
    flex: 1;
    min-height: 0;
    position: relative;
  }

  .stats-bar {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .stat {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .stops-btn {
    margin-left: auto;
    background: none;
    border: none;
    color: var(--text-dim);
    font: inherit;
    font-size: var(--text-xs);
    cursor: pointer;
    padding: 0;
  }

  .stops-btn:hover {
    color: var(--text-primary);
  }

  .chip-count {
    font-weight: 500;
    opacity: 0.8;
    margin-left: var(--space-1);
  }

  .discover-hint {
    position: absolute;
    bottom: 3rem;
    left: 50%;
    transform: translateX(-50%);
    /* Shrink-to-fit for an abs-positioned box with only `left` set is capped at
       half the container, so the text wrapped long before it had to. */
    width: max-content;
    max-width: min(90%, 34rem);
    text-align: center;
    /* Dark pill in BOTH themes — it floats over the map canvas, whose
	     lightness the theme does not control — so its text is fixed too. */
    /* design-lint-allow: fixed chrome — see the comment above; the pill floats
       over the map canvas, whose lightness the theme does not control. */
    background: rgba(17, 17, 17, 0.9);
    border: 1px solid var(--border-default);
    color: var(--on-scrim-fg);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-pill);
    pointer-events: none;
    z-index: var(--z-sticky);
  }

  @media (max-width: 768px) {
    /* The preset chips are the phone affordance; the explicit range is the
       desktop one. :global because the subject is DateRangeFilter's root now,
       and Svelte prunes a selector whose subject it cannot see — silently.
       Scoped under the page's own bar, so it is placement rather than a leak.
       The picker-icon theme pair moved into the component with the rest. */
    .controls-bar :global(.date-range) {
      display: none;
    }
  }
</style>
