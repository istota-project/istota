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
	import { locationPlaces, mapFlyTo } from '$lib/stores/location';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import StopTimeline from '$lib/components/location/StopTimeline.svelte';
	import DayStats from '$lib/components/location/DayStats.svelte';
	import TripList from '$lib/components/location/TripList.svelte';
	import Chip from '$lib/components/ui/Chip.svelte';
	import { loadSetting, saveSetting } from '$lib/stores/persisted';
	import { ACTIVITY_COLORS, ACTIVITY_LABELS, ALL_ACTIVITY_TYPES } from '$lib/location-constants';

	let pings: LocationPing[] = $state([]);
	let summary: DaySummary | null = $state(null);
	let trips: Trip[] = $state([]);
	let loading = $state(false);
	let error = $state('');
	let mapComponent: LocationMap | undefined = $state();

	let dateStr = $state('');
	let startStr = $state('');
	let endStr = $state('');
	let viewMode: 'day' | 'range' = $state('day');
	let showHeat = $state(loadSetting('location.showHeat', false));
	let panelOpen = $state(false);
	let activeActivityTypes: Set<string> = $state(new Set(ALL_ACTIVITY_TYPES));

	$effect(() => { saveSetting('location.showHeat', showHeat); });

	function toggleActivity(type: string) {
		const next = new Set(activeActivityTypes);
		if (next.has(type)) {
			if (next.size > 1) next.delete(type);
		} else {
			next.add(type);
		}
		activeActivityTypes = next;
	}

	let activityCounts = $derived(() => {
		const counts: Record<string, number> = {};
		for (const p of pings) {
			const t = p.activity_type ?? 'stationary';
			counts[t] = (counts[t] ?? 0) + 1;
		}
		return counts;
	});

	let places = $derived($locationPlaces);

	const today = new Date().toISOString().slice(0, 10);

	function yesterday(): string {
		const d = new Date();
		d.setDate(d.getDate() - 1);
		return d.toISOString().slice(0, 10);
	}

	function thisWeekStart(): string {
		const d = new Date();
		d.setDate(d.getDate() - d.getDay());
		return d.toISOString().slice(0, 10);
	}

	function thisMonthStart(): string {
		const d = new Date();
		return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-01`;
	}

	function readUrlParams() {
		const params = page.url.searchParams;
		const d = params.get('date');
		const s = params.get('start');
		const e = params.get('end');

		if (d) {
			dateStr = d;
			viewMode = 'day';
		} else if (s && e) {
			startStr = s;
			endStr = e;
			viewMode = 'range';
		} else {
			dateStr = today;
			viewMode = 'day';
		}
	}

	function updateUrl() {
		const params = new URLSearchParams();
		if (viewMode === 'day' && dateStr) {
			params.set('date', dateStr);
		} else if (viewMode === 'range' && startStr && endStr) {
			params.set('start', startStr);
			params.set('end', endStr);
		}
		goto(`${base}/location/history?${params.toString()}`, { replaceState: true, noScroll: true });
	}

	async function loadData() {
		loading = true;
		error = '';
		pings = [];
		summary = null;
		trips = [];

		try {
			if (viewMode === 'day' && dateStr) {
				const [p, s, t] = await Promise.all([
					getLocationPings({ date: dateStr }),
					getDaySummary(dateStr),
					getTrips(dateStr),
				]);
				pings = p.pings;
				summary = s;
				trips = t.trips;
				panelOpen = s.stops.length > 0 || t.trips.length > 0;
			} else if (viewMode === 'range' && startStr && endStr) {
				const p = await getLocationPings({ start: startStr, end: endStr, limit: '50000' });
				pings = p.pings;
			}
		} catch {
			error = 'Failed to load location data';
		} finally {
			loading = false;
		}
	}

	function selectDay(date: string) {
		viewMode = 'day';
		dateStr = date;
		showHeat = false;
		updateUrl();
		loadData();
	}

	function selectRange(start: string, end: string) {
		viewMode = 'range';
		startStr = start;
		endStr = end;
		showHeat = false;
		updateUrl();
		loadData();
	}

	function handleDateInput() {
		if (dateStr) {
			viewMode = 'day';
			showHeat = false;
			updateUrl();
			loadData();
		}
	}

	function handleRangeInput() {
		if (startStr && endStr) {
			viewMode = 'range';
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
		mapFlyTo.set(null);
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
			<Chip checked={viewMode === 'day' && dateStr === today} onclick={() => selectDay(today)}>Today</Chip>
			<Chip checked={viewMode === 'day' && dateStr === yesterday()} onclick={() => selectDay(yesterday())}>Yesterday</Chip>
			<Chip onclick={() => selectRange(thisWeekStart(), today)}>This week</Chip>
			<Chip onclick={() => selectRange(thisMonthStart(), today)}>This month</Chip>
		</div>
		<div class="date-inputs">
			<label for="hist-date">Date</label>
			<input id="hist-date" type="date" bind:value={dateStr} onchange={handleDateInput} max={today} />
			<span class="sep">or</span>
			<label for="hist-start">From</label>
			<input id="hist-start" type="date" bind:value={startStr} onchange={handleRangeInput} max={today} />
			<label for="hist-end">To</label>
			<input id="hist-end" type="date" bind:value={endStr} onchange={handleRangeInput} max={today} />
		</div>
		{#if viewMode === 'range' && pings.length > 0}
			<Chip checked={showHeat} onclick={() => showHeat = !showHeat}>Heat map</Chip>
		{/if}
		{#if pings.length > 0}
			<div class="chip-group activity-chips">
				{#each ALL_ACTIVITY_TYPES as type}
					{@const count = activityCounts()[type] ?? 0}
					{#if count > 0}
						<Chip checked={activeActivityTypes.has(type)} onclick={() => toggleActivity(type)}>
							<span class="activity-dot" style="background: {ACTIVITY_COLORS[type]}"></span>
							{ACTIVITY_LABELS[type]}
							<span class="chip-count">{count}</span>
						</Chip>
					{/if}
				{/each}
			</div>
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
				stops={summary?.stops ?? []}
				{places}
				showPath={!showHeat}
				{showHeat}
				{activeActivityTypes}
				onStopClick={handleStopClick}
			/>
		</div>

		<div class="stats-bar">
			<span class="stat">{pings.length} pings</span>
			{#if summary}
				<span class="stat">{summary.stops.length} stops</span>
				<span class="stat">{summary.transit_pings} transit</span>
			{/if}
			{#if viewMode === 'range'}
				{@const uniquePlaces = new Set(pings.filter(p => p.place).map(p => p.place))}
				<span class="stat">{uniquePlaces.size} places</span>
			{/if}
			{#if viewMode === 'day' && trips.length > 0}
				<span class="stat">{trips.length} trips</span>
			{/if}
			{#if (summary && summary.stops.length > 0) || trips.length > 0}
				<button class="stops-btn" onclick={() => panelOpen = !panelOpen} type="button">
					{panelOpen ? 'Hide details' : 'Show details'}
				</button>
			{/if}
		</div>

		{#if panelOpen && viewMode === 'day'}
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

	.sep {
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

	.activity-chips {
		border-left: 1px solid var(--border-subtle);
		padding-left: 0.75rem;
	}

	.activity-dot {
		display: inline-block;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		margin-right: 0.15rem;
	}

	.chip-count {
		font-size: 0.65rem;
		opacity: 0.6;
		margin-left: 0.15rem;
	}

	@media (max-width: 768px) {
		.date-inputs { display: none; }
		.activity-chips { border-left: none; padding-left: 0; }
	}
</style>
