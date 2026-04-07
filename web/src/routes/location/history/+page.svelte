<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { onMount, onDestroy } from 'svelte';
	import {
		getLocationPings,
		getDaySummary,
		getTrips,
		type LocationPing,
		type DaySummary,
		type DaySummaryStop,
		type Trip,
	} from '$lib/api';
	import { locationPlaces, mapFlyTo, selectedPlaceId, onPlaceMove } from '$lib/stores/location';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import StopTimeline from '$lib/components/location/StopTimeline.svelte';
	import DayStats from '$lib/components/location/DayStats.svelte';
	import TripList from '$lib/components/location/TripList.svelte';
	import Chip from '$lib/components/ui/Chip.svelte';
	import { loadSetting, saveSetting } from '$lib/stores/persisted';
	import { ACTIVITY_LABELS, ALL_ACTIVITY_TYPES } from '$lib/location-constants';

	let pings: LocationPing[] = $state([]);
	let summary: DaySummary | null = $state(null);
	let trips: Trip[] = $state([]);
	let loading = $state(false);
	let error = $state('');
	let mapComponent: LocationMap | undefined = $state();

	let startStr = $state('');
	let endStr = $state('');
	let showHeat = $state(loadSetting('location.showHeat', false));
	let panelOpen = $state(false);
	let activityFilter: string = $state('all');

	$effect(() => { saveSetting('location.showHeat', showHeat); });

	let activeActivityTypes = $derived<Set<string> | null>(
		activityFilter === 'all' ? null : new Set([activityFilter])
	);

	let places = $derived($locationPlaces);

	function localDate(d: Date = new Date()): string {
		return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
	}
	const today = localDate();
	let isSingleDay = $derived(startStr === endStr);

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
		trips = [];

		try {
			if (!startStr || !endStr) return;
			if (isSingleDay) {
				const [p, s, t] = await Promise.all([
					getLocationPings({ date: startStr }),
					getDaySummary(startStr),
					getTrips(startStr),
				]);
				pings = p.pings;
				summary = s;
				trips = t.trips;
				panelOpen = s.stops.length > 0 || t.trips.length > 0;
			} else {
				const p = await getLocationPings({ start: startStr, end: endStr, limit: '50000' });
				pings = p.pings;
			}
		} catch {
			error = 'Failed to load location data';
		} finally {
			loading = false;
		}
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
		<div class="chip-group">
			<Chip checked={isSingleDay && startStr === today} onclick={() => selectRange(today, today)}>Today</Chip>
			<Chip checked={isSingleDay && startStr === yesterday()} onclick={() => selectRange(yesterday(), yesterday())}>Yesterday</Chip>
			<Chip checked={startStr === thisWeekStart() && endStr === today} onclick={() => selectRange(thisWeekStart(), today)}>This week</Chip>
			<Chip checked={startStr === thisMonthStart() && endStr === today} onclick={() => selectRange(thisMonthStart(), today)}>This month</Chip>
		</div>
		<div class="date-inputs">
			<label for="hist-start">From</label>
			<input id="hist-start" type="date" bind:value={startStr} onchange={handleRangeInput} max={today} />
			<label for="hist-end">To</label>
			<input id="hist-end" type="date" bind:value={endStr} onchange={handleRangeInput} max={today} />
		</div>
		{#if !isSingleDay && pings.length > 0}
			<Chip checked={showHeat} onclick={() => showHeat = !showHeat}>Heat map</Chip>
		{/if}
		{#if pings.length > 0}
			<select class="mode-select" bind:value={activityFilter}>
				<option value="all">All</option>
				{#each ALL_ACTIVITY_TYPES as type}
					<option value={type}>{ACTIVITY_LABELS[type]}</option>
				{/each}
			</select>
		{/if}
	</div>

	{#if loading}
		<div class="center-msg">Loading...</div>
	{:else if error}
		<div class="center-msg error">{error}</div>
	{:else if pings.length === 0}
		<div class="center-msg">No location data for this period</div>
	{:else}
		<div class="map-area">
			<LocationMap
				bind:this={mapComponent}
				{pings}
				{places}
				showPath={!showHeat}
				{showHeat}
				{activeActivityTypes}
				selectedPlaceId={$selectedPlaceId}
				onPlaceMove={$onPlaceMove}
			/>
		</div>

		<div class="stats-bar">
			<span class="stat">{pings.length} pings</span>
			{#if summary}
				<span class="stat">{summary.stops.length} stops</span>
				<span class="stat">{summary.transit_pings} transit</span>
			{/if}
			{#if !isSingleDay}
				{@const uniquePlaces = new Set(pings.filter(p => p.place).map(p => p.place))}
				<span class="stat">{uniquePlaces.size} places</span>
			{/if}
			{#if isSingleDay && trips.length > 0}
				<span class="stat">{trips.length} trips</span>
			{/if}
			{#if (summary && summary.stops.length > 0) || trips.length > 0}
				<button class="stops-btn" onclick={() => panelOpen = !panelOpen} type="button">
					{panelOpen ? 'Hide details' : 'Show details'}
				</button>
			{/if}
		</div>

		{#if panelOpen && isSingleDay}
			<div class="stops-panel">
				{#if pings.length > 1}
					<div class="panel-section">
						<DayStats {pings} />
					</div>
				{/if}
				{#if trips.length > 0}
					<div class="panel-section">
						<div class="panel-label">Trips</div>
						<TripList {trips} onTripClick={handleTripClick} />
					</div>
				{/if}
				{#if summary && summary.stops.length > 0}
					<div class="panel-section">
						<div class="panel-label">Stops</div>
						<StopTimeline stops={summary.stops} onStopClick={handleStopClick} />
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</div>

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
		gap: 0.75rem;
		padding: 0.5rem 0.75rem;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	.chip-group {
		display: flex;
		gap: 0.25rem;
	}

	.date-inputs {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}

	.date-inputs label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.date-inputs input[type="date"] {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font-size: var(--text-xs);
		padding: 0.2rem 0.4rem;
		border-radius: 0.25rem;
		font-family: inherit;
	}

	.date-inputs input[type="date"]::-webkit-calendar-picker-indicator {
		filter: invert(0.7);
	}

	.map-area {
		flex: 1;
		min-height: 0;
		position: relative;
	}

	.center-msg {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--text-dim);
		font-size: var(--text-sm);
	}

	.center-msg.error { color: #c66; }

	.stats-bar {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 0.4rem 0.75rem;
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

	.stops-btn:hover { color: var(--text-primary); }

	.stops-panel {
		max-height: 200px;
		overflow-y: auto;
		border-top: 1px solid var(--border-subtle);
		padding: 0.5rem 0.75rem;
		flex-shrink: 0;
	}

	.stops-panel::-webkit-scrollbar { width: 3px; }
	.stops-panel::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.panel-section {
		padding-bottom: 0.5rem;
		margin-bottom: 0.25rem;
		border-bottom: 1px solid var(--border-subtle);
	}

	.panel-section:last-child {
		border-bottom: none;
		margin-bottom: 0;
		padding-bottom: 0;
	}

	.panel-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		margin-bottom: 0.25rem;
	}

	.mode-select {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.2rem 0.4rem;
		border-radius: 0.25rem;
		cursor: pointer;
	}

	@media (max-width: 768px) {
		.date-inputs { display: none; }
	}
</style>
