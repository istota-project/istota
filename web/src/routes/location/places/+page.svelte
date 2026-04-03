<script lang="ts">
	import { base } from '$app/paths';
	import { onMount } from 'svelte';
	import { getLocationPlaces, type Place } from '$lib/api';
	import LocationMap from '$lib/components/location/LocationMap.svelte';

	let places: Place[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let selectedPlace: Place | null = $state(null);
	let mapComponent: LocationMap | undefined = $state();

	let groupedPlaces = $derived.by(() => {
		const groups: Record<string, Place[]> = {};
		for (const p of places) {
			const cat = p.category || 'other';
			if (!groups[cat]) groups[cat] = [];
			groups[cat].push(p);
		}
		return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
	});

	async function loadData() {
		try {
			const resp = await getLocationPlaces();
			places = resp.places;
		} catch {
			error = 'Failed to load places';
		} finally {
			loading = false;
		}
	}

	function handlePlaceClick(place: Place) {
		selectedPlace = place;
		mapComponent?.flyTo(place.lat, place.lon, 16);
	}

	onMount(() => {
		loadData();
	});
</script>

<div class="location-page">
	<div class="page-header">
		<h1>Location</h1>
		<div class="nav-links">
			<a href="{base}/location">Today</a>
			<a href="{base}/location/history">History</a>
			<a href="{base}/location/places" class="active">Places</a>
		</div>
	</div>

	{#if loading}
		<div class="loading">Loading places...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else if places.length === 0}
		<div class="loading">No saved places</div>
	{:else}
		<div class="layout">
			<div class="map-panel">
				<LocationMap
					bind:this={mapComponent}
					{places}
					pings={[]}
					stops={[]}
					showPath={false}
				/>
			</div>
			<div class="sidebar">
				<div class="place-count">{places.length} places</div>
				{#each groupedPlaces as [category, catPlaces]}
					<div class="category-group">
						<div class="category-label">{category}</div>
						{#each catPlaces as place}
							<button
								class="place-item"
								class:selected={selectedPlace?.name === place.name}
								onclick={() => handlePlaceClick(place)}
								type="button"
							>
								<span class="place-name">{place.name}</span>
								<span class="place-radius">{place.radius_meters}m</span>
							</button>
						{/each}
					</div>
				{/each}
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

	.nav-links a:hover { color: var(--text-primary); }
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
		gap: 0.75rem;
	}

	.place-count {
		font-size: var(--text-sm);
		color: var(--text-dim);
	}

	.category-group {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.category-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		padding: 0.25rem 0.5rem;
	}

	.place-item {
		display: flex;
		justify-content: space-between;
		align-items: center;
		background: none;
		border: none;
		color: inherit;
		font: inherit;
		cursor: pointer;
		padding: 0.35rem 0.5rem;
		border-radius: var(--radius-card);
		transition: background var(--transition-fast);
		text-align: left;
	}

	.place-item:hover {
		background: var(--surface-raised);
	}

	.place-item.selected {
		background: var(--surface-card);
	}

	.place-name {
		font-size: var(--text-base);
	}

	.place-radius {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	@media (max-width: 768px) {
		.layout { grid-template-columns: 1fr; }
		.map-panel { height: 50vh; min-height: 300px; }
	}
</style>
