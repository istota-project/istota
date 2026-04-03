<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { locationPlaces, mapFlyTo } from '$lib/stores/location';
	import LocationMap from '$lib/components/location/LocationMap.svelte';

	let mapComponent: LocationMap | undefined = $state();
	let places = $derived($locationPlaces);

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
			pings={[]}
			stops={[]}
			showPath={false}
		/>
	</div>
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
</style>
