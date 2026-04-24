<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { locationPlaces, reloadPlaces, mapFlyTo, selectedPlaceId, onPlaceMove } from '$lib/stores/location';
	import {
		discoverPlaces,
		createPlace,
		listDismissedClusters,
		dismissCluster,
		restoreDismissedCluster,
		type DiscoveredCluster,
		type DismissedCluster,
	} from '$lib/api';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import PlaceForm from '$lib/components/location/PlaceForm.svelte';

	let mapComponent: LocationMap | undefined = $state();
	let places = $derived($locationPlaces);
	let clusters: DiscoveredCluster[] = $state([]);
	let dismissed: DismissedCluster[] = $state([]);
	let selectedCluster: DiscoveredCluster | null = $state(null);
	let pendingPick: { lat: number; lon: number } | null = $state(null);
	let picking = $state(false);
	let showDismissed = $state(false);
	let loading = $state(false);
	let error = $state('');

	async function loadClusters() {
		loading = true;
		try {
			const [discoverResult, dismissedResult] = await Promise.all([
				discoverPlaces(),
				listDismissedClusters(),
			]);
			clusters = discoverResult.clusters;
			dismissed = dismissedResult.dismissed;
		} catch {
			error = 'Failed to discover places';
		} finally {
			loading = false;
		}
	}

	function handleClusterClick(cluster: DiscoveredCluster) {
		if (picking) return;
		selectedCluster = cluster;
		mapComponent?.flyTo(cluster.lat, cluster.lon, 16);
	}

	function handleMapClick(lat: number, lon: number) {
		if (!picking) return;
		picking = false;
		pendingPick = { lat, lon };
	}

	function toggleNewPlace() {
		if (selectedCluster || pendingPick) return;
		picking = !picking;
	}

	function closeForm() {
		selectedCluster = null;
		pendingPick = null;
	}

	async function handleSave(data: { name: string; lat: number; lon: number; radius_meters: number; category: string; notes: string }) {
		try {
			await createPlace(data);
			closeForm();
			await reloadPlaces();
			await loadClusters();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save place';
		}
	}

	async function handleDismiss(data: { lat: number; lon: number; radius_meters: number }) {
		try {
			await dismissCluster(data);
			closeForm();
			await loadClusters();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to dismiss cluster';
		}
	}

	async function handleDismissedClick(d: DismissedCluster) {
		if (!showDismissed || picking) return;
		const ok = confirm('Restore this dismissed area? Future pings here may form a cluster again.');
		if (!ok) return;
		try {
			await restoreDismissedCluster(d.id);
			await loadClusters();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to restore';
		}
	}

	onMount(() => {
		loadClusters();
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
	<div class="map-fill">
		<LocationMap
			bind:this={mapComponent}
			{places}
			{clusters}
			dismissedClusters={showDismissed ? dismissed : []}
			pings={[]}
			showPath={false}
			onClusterClick={handleClusterClick}
			onDismissedClusterClick={handleDismissedClick}
			selectedPlaceId={$selectedPlaceId}
			onPlaceMove={$onPlaceMove}
			pickingLocation={picking}
			onMapClick={handleMapClick}
		/>
	</div>

	<div class="badges">
		<button
			class="toggle-badge new-place"
			class:active={picking}
			onclick={toggleNewPlace}
			type="button"
			title={picking ? 'Click anywhere on the map to set the location' : 'Add a new place'}
		>
			{picking ? 'Click on map…' : '+ New place'}
		</button>
		{#if clusters.length > 0 && !selectedCluster}
			<div class="discover-badge">
				{clusters.length} unknown {clusters.length === 1 ? 'place' : 'places'} detected
			</div>
		{/if}
		{#if dismissed.length > 0}
			<button
				class="toggle-badge"
				class:active={showDismissed}
				onclick={() => (showDismissed = !showDismissed)}
				type="button"
				title={showDismissed ? 'Click to hide' : 'Click to show on map'}
			>
				{showDismissed ? 'Hide' : 'Show'} {dismissed.length} dismissed
			</button>
		{/if}
	</div>

	{#if error}
		<div class="error-badge">{error}</div>
	{/if}

	{#if selectedCluster}
		<PlaceForm
			cluster={selectedCluster}
			onSave={handleSave}
			onDismiss={handleDismiss}
			onCancel={closeForm}
		/>
	{:else if pendingPick}
		<PlaceForm
			initialLat={pendingPick.lat}
			initialLon={pendingPick.lon}
			onSave={handleSave}
			onCancel={closeForm}
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

	.badges {
		position: absolute;
		bottom: 1rem;
		left: 50%;
		transform: translateX(-50%);
		z-index: 10;
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}

	.discover-badge {
		background: rgba(17, 17, 17, 0.9);
		border: 1px solid #ffc107;
		color: #ffc107;
		font-size: var(--text-xs);
		padding: 0.35rem 0.75rem;
		border-radius: var(--radius-pill);
		backdrop-filter: blur(8px);
	}

	.toggle-badge {
		background: rgba(17, 17, 17, 0.9);
		border: 1px solid #555;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.35rem 0.75rem;
		border-radius: var(--radius-pill);
		backdrop-filter: blur(8px);
		cursor: pointer;
	}

	.toggle-badge:hover { color: var(--text-primary); border-color: #777; }
	.toggle-badge.active { color: var(--text-primary); border-color: #888; }

	.new-place.active {
		color: #ffc107;
		border-color: #ffc107;
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
