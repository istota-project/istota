<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		getLocationCurrent,
		getLocationPings,
		getDaySummary,
		getTrips,
		type CurrentLocation,
		type LocationPing,
		type DaySummary,
		type DaySummaryStop,
		type Trip,
	} from '$lib/api';
	import {
		locationPlaces,
		mapFlyTo,
		selectedPlaceId,
		onPlaceMove,
		pickingPlace,
		requestNewPlace,
	} from '$lib/stores/location';
	import { loadSetting, saveSetting } from '$lib/stores/persisted';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import StopTimeline from '$lib/components/location/StopTimeline.svelte';
	import DayStats from '$lib/components/location/DayStats.svelte';
	import TripList from '$lib/components/location/TripList.svelte';

	let current = $state<CurrentLocation | null>(null);
	let pings: LocationPing[] = $state([]);
	let summary: DaySummary | null = $state(null);
	let trips: Trip[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let pollInterval: ReturnType<typeof setInterval> | undefined;
	let mapComponent: LocationMap | undefined = $state();
	let panelOpen = $state(loadSetting('location.panelOpen', false));

	$effect(() => { saveSetting('location.panelOpen', panelOpen); });

	function localDate(d: Date = new Date()): string {
		return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
	}
	const today = localDate();

	let places = $derived($locationPlaces);

	let currentPos = $derived(
		current?.last_ping
			? { lat: current.last_ping.lat, lon: current.last_ping.lon }
			: null
	);

	function formatDuration(minutes: number | null): string {
		if (minutes == null) return '';
		if (minutes < 60) return `${minutes}m`;
		const h = Math.floor(minutes / 60);
		const m = minutes % 60;
		return m > 0 ? `${h}h ${m}m` : `${h}h`;
	}

	function timeAgo(timestamp: string): string {
		const diff = Date.now() - new Date(timestamp).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	let currentLabel = $derived.by(() => {
		if (!current?.last_ping) return null;
		const placeName =
			current.current_visit?.place_name ?? current.last_ping.place ?? null;
		const visitDuration = current.current_visit
			? formatDuration(current.current_visit.duration_minutes)
			: '';
		return {
			placeName,
			visitDuration,
			ago: timeAgo(current.last_ping.timestamp),
			battery:
				current.last_ping.battery != null
					? `${Math.round(current.last_ping.battery * 100)}%`
					: '',
		};
	});

	let hasDetails = $derived(
		(summary?.stops.length ?? 0) > 0 || trips.length > 0 || pings.length > 1
	);

	async function loadData() {
		try {
			const [c, p, s, t] = await Promise.all([
				getLocationCurrent(),
				getLocationPings({ date: today }),
				getDaySummary(today),
				getTrips(today),
			]);
			current = c;
			pings = p.pings;
			summary = s;
			trips = t.trips;
		} catch {
			error = 'Failed to load location data';
		} finally {
			loading = false;
		}
	}

	async function refreshCurrent() {
		try {
			current = await getLocationCurrent();
		} catch {
			// ignore
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
		loadData();
		pollInterval = setInterval(refreshCurrent, 60000);
	});

	onDestroy(() => {
		if (pollInterval) clearInterval(pollInterval);
		mapFlyTo.set(undefined);
	});

	$effect(() => {
		if (mapComponent) {
			mapFlyTo.set((lat, lon, zoom) => mapComponent?.flyTo(lat, lon, zoom));
		}
	});
</script>

<div class="page-fill">
	{#if loading}
		<div class="center-msg">Loading location data...</div>
	{:else if error}
		<div class="center-msg error">{error}</div>
	{:else}
		<div class="map-area">
			<LocationMap
				bind:this={mapComponent}
				{pings}
				{places}
				currentPosition={currentPos}
				showPath={true}
				selectedPlaceId={$selectedPlaceId}
				onPlaceMove={$onPlaceMove}
				pickingLocation={$pickingPlace}
				onMapClick={(lat, lon) => $requestNewPlace?.({ lat, lon })}
			/>
		</div>

		<div class="stats-bar">
			{#if currentLabel}
				<span class="current">
					{#if currentLabel.placeName}
						<span class="place">{currentLabel.placeName}</span>
						{#if currentLabel.visitDuration}
							<span class="stat">{currentLabel.visitDuration}</span>
						{/if}
					{:else}
						<span class="place dim">No place</span>
					{/if}
					<span class="stat">{currentLabel.ago}</span>
					{#if currentLabel.battery}
						<span class="stat">{currentLabel.battery}</span>
					{/if}
				</span>
			{:else}
				<span class="stat dim">No location data</span>
			{/if}
			{#if pings.length > 0}
				<span class="stat">{pings.length} pings</span>
			{/if}
			{#if summary && summary.stops.length > 0}
				<span class="stat">{summary.stops.length} stops</span>
			{/if}
			{#if summary && summary.transit_pings > 0}
				<span class="stat">{summary.transit_pings} transit</span>
			{/if}
			{#if trips.length > 0}
				<span class="stat">{trips.length} trips</span>
			{/if}
			{#if hasDetails}
				<button class="stops-btn" onclick={() => panelOpen = !panelOpen} type="button">
					{panelOpen ? 'Hide details' : 'Show details'}
				</button>
			{/if}
		</div>

		{#if panelOpen && hasDetails}
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
		flex-wrap: wrap;
	}

	.current {
		display: inline-flex;
		align-items: baseline;
		gap: 0.5rem;
	}

	.place {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-primary);
	}

	.place.dim {
		color: var(--text-dim);
		font-weight: 400;
		font-size: var(--text-xs);
	}

	.stat {
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	.stat.dim {
		color: var(--text-dim);
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
</style>
