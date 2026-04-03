<script lang="ts">
	import { base } from '$app/paths';
	import { onMount, onDestroy } from 'svelte';
	import {
		getLocationCurrent,
		getLocationPings,
		getDaySummary,
		getLocationPlaces,
		type CurrentLocation,
		type LocationPing,
		type DaySummary,
		type DaySummaryStop,
		type Place,
	} from '$lib/api';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import CurrentStatus from '$lib/components/location/CurrentStatus.svelte';
	import StopTimeline from '$lib/components/location/StopTimeline.svelte';

	let current: CurrentLocation | null = $state(null);
	let pings: LocationPing[] = $state([]);
	let summary: DaySummary | null = $state(null);
	let places: Place[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let pollInterval: ReturnType<typeof setInterval> | undefined;
	let mapComponent: LocationMap | undefined = $state();

	const today = new Date().toISOString().slice(0, 10);

	let currentPos = $derived(
		current?.last_ping
			? { lat: current.last_ping.lat, lon: current.last_ping.lon }
			: null
	);

	async function loadData() {
		try {
			const [c, p, s, pl] = await Promise.all([
				getLocationCurrent(),
				getLocationPings({ date: today }),
				getDaySummary(today),
				getLocationPlaces(),
			]);
			current = c;
			pings = p.pings;
			summary = s;
			places = pl.places;
		} catch (e) {
			error = 'Failed to load location data';
		} finally {
			loading = false;
		}
	}

	async function refreshCurrent() {
		try {
			current = await getLocationCurrent();
		} catch {
			// ignore polling errors
		}
	}

	function handleStopClick(stop: DaySummaryStop) {
		mapComponent?.flyTo(stop.lat, stop.lon);
	}

	onMount(() => {
		loadData();
		pollInterval = setInterval(refreshCurrent, 60000);
	});

	onDestroy(() => {
		if (pollInterval) clearInterval(pollInterval);
	});
</script>

<div class="location-page">
	<div class="page-header">
		<h1>Location</h1>
		<div class="nav-links">
			<a href="{base}/location" class="active">Today</a>
			<a href="{base}/location/history">History</a>
			<a href="{base}/location/places">Places</a>
		</div>
	</div>

	{#if loading}
		<div class="loading">Loading location data...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else}
		<div class="layout">
			<div class="map-panel">
				<LocationMap
					bind:this={mapComponent}
					{pings}
					stops={summary?.stops ?? []}
					{places}
					currentPosition={currentPos}
					showPath={true}
					onStopClick={handleStopClick}
				/>
			</div>
			<div class="sidebar">
				<div class="sidebar-section">
					<div class="section-label">Current</div>
					<CurrentStatus {current} />
				</div>
				{#if summary && summary.stops.length > 0}
					<div class="sidebar-section">
						<div class="section-label">
							Stops
							{#if summary.ping_count}
								<span class="ping-count">{summary.ping_count} pings</span>
							{/if}
						</div>
						<StopTimeline stops={summary.stops} onStopClick={handleStopClick} />
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.location-page {
		max-width: 1200px;
		margin: 0 auto;
	}

	.page-header {
		display: flex;
		align-items: baseline;
		gap: 1.5rem;
		margin-bottom: 1rem;
	}

	.page-header h1 {
		font-size: 1.1rem;
		font-weight: 600;
		margin: 0;
	}

	.nav-links {
		display: flex;
		gap: 0.5rem;
	}

	.nav-links a {
		font-size: var(--text-sm);
		color: var(--text-muted);
		text-decoration: none;
		padding: 0.2rem 0.6rem;
		border-radius: var(--radius-pill);
		transition: all var(--transition-fast);
	}

	.nav-links a:hover {
		color: var(--text-primary);
	}

	.nav-links a.active {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.layout {
		display: grid;
		grid-template-columns: 1fr 280px;
		gap: 1rem;
		align-items: start;
	}

	.map-panel {
		height: 60vh;
		min-height: 400px;
		border-radius: var(--radius-card);
		overflow: hidden;
	}

	.sidebar {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.sidebar-section {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.section-label {
		font-size: var(--text-sm);
		color: var(--text-dim);
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
	}

	.ping-count {
		font-weight: 400;
		text-transform: none;
		letter-spacing: 0;
	}

	@media (max-width: 768px) {
		.layout {
			grid-template-columns: 1fr;
		}

		.map-panel {
			height: 50vh;
			min-height: 300px;
		}
	}
</style>
