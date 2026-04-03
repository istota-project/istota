<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { locationPlaces, reloadPlaces, mapFlyTo } from '$lib/stores/location';
	import {
		discoverPlaces,
		createPlace,
		type DiscoveredCluster,
	} from '$lib/api';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import PlaceForm from '$lib/components/location/PlaceForm.svelte';

	let mapComponent: LocationMap | undefined = $state();
	let places = $derived($locationPlaces);
	let clusters: DiscoveredCluster[] = $state([]);
	let selectedCluster: DiscoveredCluster | null = $state(null);
	let loading = $state(false);
	let error = $state('');

	async function loadClusters() {
		loading = true;
		try {
			const result = await discoverPlaces();
			clusters = result.clusters;
		} catch {
			error = 'Failed to discover places';
		} finally {
			loading = false;
		}
	}

	function handleClusterClick(cluster: DiscoveredCluster) {
		selectedCluster = cluster;
		mapComponent?.flyTo(cluster.lat, cluster.lon, 16);
	}

	async function handleSave(data: { name: string; lat: number; lon: number; radius_meters: number; category: string }) {
		try {
			await createPlace(data);
			selectedCluster = null;
			await reloadPlaces();
			await loadClusters();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save place';
		}
	}

	onMount(() => {
		loadClusters();
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
	<div class="map-fill">
		<LocationMap
			bind:this={mapComponent}
			{places}
			{clusters}
			pings={[]}
			stops={[]}
			showPath={false}
			onClusterClick={handleClusterClick}
		/>
	</div>

	{#if clusters.length > 0 && !selectedCluster}
		<div class="discover-badge">
			{clusters.length} unknown {clusters.length === 1 ? 'place' : 'places'} detected
		</div>
	{/if}

	{#if error}
		<div class="error-badge">{error}</div>
	{/if}

	{#if selectedCluster}
		<PlaceForm
			cluster={selectedCluster}
			onSave={handleSave}
			onCancel={() => selectedCluster = null}
		/>
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

	.discover-badge {
		position: absolute;
		bottom: 1rem;
		left: 50%;
		transform: translateX(-50%);
		z-index: 10;
		background: rgba(17, 17, 17, 0.9);
		border: 1px solid #ffc107;
		color: #ffc107;
		font-size: var(--text-xs);
		padding: 0.35rem 0.75rem;
		border-radius: var(--radius-pill);
		backdrop-filter: blur(8px);
	}

	.error-badge {
		position: absolute;
		top: 1rem;
		left: 50%;
		transform: translateX(-50%);
		z-index: 10;
		background: rgba(17, 17, 17, 0.9);
		border: 1px solid #c66;
		color: #c66;
		font-size: var(--text-xs);
		padding: 0.35rem 0.75rem;
		border-radius: var(--radius-pill);
	}
</style>
