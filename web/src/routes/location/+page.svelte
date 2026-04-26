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
	import { locationPlaces, mapFlyTo, selectedPlaceId, onPlaceMove } from '$lib/stores/location';
	import { loadSetting, saveSetting } from '$lib/stores/persisted';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import CurrentStatus from '$lib/components/location/CurrentStatus.svelte';
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
	let panelOpen = $state(loadSetting('location.panelOpen', true));

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
		<div class="loading">Loading location data...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else}
		<div class="map-fill">
			<LocationMap
				bind:this={mapComponent}
				{pings}
				{places}
				currentPosition={currentPos}
				showPath={true}
				selectedPlaceId={$selectedPlaceId}
				onPlaceMove={$onPlaceMove}
			/>
		</div>

		<div class="info-panel" class:collapsed={!panelOpen}>
			<button class="panel-toggle" onclick={() => panelOpen = !panelOpen} type="button">
				{panelOpen ? 'Hide' : 'Info'}
				{#if !panelOpen && summary}
					({summary.stops.length} stops)
				{/if}
			</button>
			{#if panelOpen}
				<div class="panel-content">
					<CurrentStatus {current} />
					{#if pings.length > 1}
						<div class="section">
							<div class="section-label">Stats</div>
							<DayStats {pings} />
						</div>
					{/if}
					{#if trips.length > 0}
						<div class="section">
							<div class="section-label">Trips <span class="meta">{trips.length}</span></div>
							<TripList {trips} onTripClick={handleTripClick} />
						</div>
					{/if}
					{#if summary && summary.stops.length > 0}
						<div class="section">
							<div class="section-label">
								Stops
								{#if summary.ping_count}
									<span class="meta">{summary.ping_count} pings</span>
								{/if}
							</div>
							<StopTimeline stops={summary.stops} onStopClick={handleStopClick} />
						</div>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.page-fill {
		flex: 1;
		display: flex;
		position: relative;
		min-height: 0;
	}

	.map-fill {
		position: absolute;
		inset: 0;
	}

	.info-panel {
		position: absolute;
		bottom: 0.5rem;
		left: 0.5rem;
		z-index: 10;
		background: rgba(17, 17, 17, 0.92);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		max-width: 260px;
		max-height: calc(100% - 1rem);
		display: flex;
		flex-direction: column;
		overflow: hidden;
		backdrop-filter: blur(8px);
	}

	.info-panel.collapsed {
		max-width: none;
	}

	.panel-toggle {
		background: none;
		border: none;
		border-bottom: 1px solid var(--border-subtle);
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.35rem 0.6rem;
		cursor: pointer;
		text-align: left;
		flex-shrink: 0;
	}

	.panel-toggle:hover { color: var(--text-primary); }

	.panel-content {
		overflow-y: auto;
		padding: 0.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.panel-content::-webkit-scrollbar { width: 3px; }
	.panel-content::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.section {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}

	.section-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
	}

	.meta {
		font-weight: 400;
		text-transform: none;
		letter-spacing: 0;
	}

	@media (max-width: 768px) {
		.info-panel {
			right: 0.5rem;
			max-width: none;
			max-height: 40%;
		}
	}
</style>
