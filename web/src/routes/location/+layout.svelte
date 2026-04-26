<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { deletePlace, updatePlace, getPlaceStats, type Place, type PlaceStats } from '$lib/api';
	import {
		locationPlaces,
		reloadPlaces,
		mapFlyTo,
		selectedPlaceId as selectedPlaceIdStore,
		onPlaceMove as onPlaceMoveStore,
	} from '$lib/stores/location';
	import PlaceForm from '$lib/components/location/PlaceForm.svelte';
	import {
		AppShell,
		ShellHeader,
		Sidebar,
		SidebarToggle,
		CategoryGroup,
		NavLink,
	} from '$lib/components/ui';

	let { children } = $props();

	let places = $derived($locationPlaces);
	let sidebarOpen = $state(false);
	let selectedPlace: Place | null = $state(null);
	let placeStats: PlaceStats | null = $state(null);
	let statsLoading = $state(false);
	let editingPlace: Place | null = $state(null);

	function isActive(path: string): boolean {
		return page.url.pathname.startsWith(`${base}${path}`);
	}

	function isExactActive(path: string): boolean {
		const current = page.url.pathname;
		return current === `${base}${path}` || current === `${base}${path}/`;
	}

	async function handlePlaceClick(place: Place) {
		const fly = $mapFlyTo;
		if (fly) fly(place.lat, place.lon, 15);

		if (selectedPlace?.id === place.id) {
			selectedPlace = null;
			placeStats = null;
			return;
		}

		selectedPlace = place;
		placeStats = null;
		statsLoading = true;
		try {
			placeStats = await getPlaceStats(place.id);
		} catch {
			placeStats = null;
		} finally {
			statsLoading = false;
		}
	}

	function handleEditPlace(place: Place) {
		editingPlace = place;
	}

	async function handleEditSave(data: {
		name: string;
		lat: number;
		lon: number;
		radius_meters: number;
		category: string;
		notes: string;
	}) {
		if (!editingPlace) return;
		try {
			await updatePlace(editingPlace.id, data);
			editingPlace = null;
			selectedPlace = null;
			placeStats = null;
			await reloadPlaces();
		} catch {
			// ignore
		}
	}

	async function handleDeletePlace(place: Place) {
		try {
			await deletePlace(place.id);
			if (selectedPlace?.id === place.id) {
				selectedPlace = null;
				placeStats = null;
			}
			if (editingPlace?.id === place.id) {
				editingPlace = null;
			}
			await reloadPlaces();
		} catch {
			// ignore
		}
	}

	let groupedPlaces = $derived.by(() => {
		const groups: Record<string, Place[]> = {};
		for (const p of places) {
			const cat = p.category || 'other';
			if (!groups[cat]) groups[cat] = [];
			groups[cat].push(p);
		}
		return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
	});

	function formatDuration(minutes: number | null): string {
		if (minutes == null) return '—';
		if (minutes < 60) return `${minutes}m`;
		const h = Math.floor(minutes / 60);
		const m = minutes % 60;
		return m ? `${h}h ${m}m` : `${h}h`;
	}

	function formatDate(iso: string | null): string {
		if (!iso) return '—';
		try {
			const d = new Date(iso + (iso.includes('T') ? '' : 'T00:00:00Z'));
			return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
		} catch {
			return iso;
		}
	}

	async function handlePlaceMove(placeId: number, lat: number, lon: number) {
		try {
			await updatePlace(placeId, { lat, lon });
			await reloadPlaces();
			if (selectedPlace?.id === placeId) {
				placeStats = null;
				statsLoading = true;
				try {
					placeStats = await getPlaceStats(placeId);
				} catch {
					placeStats = null;
				} finally {
					statsLoading = false;
				}
			}
		} catch {
			await reloadPlaces();
		}
	}

	$effect(() => {
		selectedPlaceIdStore.set(selectedPlace?.id ?? null);
	});

	$effect(() => {
		onPlaceMoveStore.set(handlePlaceMove);
		return () => onPlaceMoveStore.set(undefined);
	});

	function handleVisibility() {
		if (document.visibilityState === 'visible') {
			reloadPlaces().catch(() => {});
		}
	}

	onMount(() => {
		reloadPlaces().catch(() => {});
		document.addEventListener('visibilitychange', handleVisibility);
		return () => document.removeEventListener('visibilitychange', handleVisibility);
	});
</script>

<AppShell>
	{#snippet header()}
		<ShellHeader title="Location">
			{#snippet nav()}
				<NavLink href="{base}/location" active={isExactActive('/location')}>Today</NavLink>
				<NavLink href="{base}/location/history" active={isActive('/location/history')}>History</NavLink>
				<NavLink href="{base}/location/places" active={isActive('/location/places')}>Places</NavLink>
			{/snippet}
			{#snippet tools()}
				<SidebarToggle
					open={sidebarOpen}
					label="Places"
					count={places.length}
					onclick={() => (sidebarOpen = !sidebarOpen)}
				/>
			{/snippet}
		</ShellHeader>
	{/snippet}

	{#snippet sidebar()}
		<Sidebar
			title="Places"
			count={places.length}
			open={sidebarOpen}
			onClose={() => (sidebarOpen = false)}
		>
			{#snippet extras()}
				{#if selectedPlace && (statsLoading || placeStats)}
					<div class="stats-panel">
						<div class="stats-header">
							<span class="stats-name">{selectedPlace.name}</span>
							<div class="stats-actions">
								<button
									class="stats-edit"
									onclick={() => handleEditPlace(selectedPlace!)}
									type="button"
									title="Edit place">&#9998;</button
								>
								<button
									class="stats-close"
									onclick={() => {
										selectedPlace = null;
										placeStats = null;
									}}
									type="button">&times;</button
								>
							</div>
						</div>
						{#if selectedPlace.notes}
							<div class="stats-notes">{selectedPlace.notes}</div>
						{/if}
						{#if statsLoading}
							<div class="stats-loading">Loading...</div>
						{:else if placeStats && placeStats.total_visits > 0}
							<div class="stats-grid">
								<div class="stat">
									<span class="stat-value">{placeStats.total_visits}</span>
									<span class="stat-label">{placeStats.total_visits === 1 ? 'visit' : 'visits'}</span>
								</div>
								<div class="stat">
									<span class="stat-value">{formatDuration(placeStats.avg_duration_min)}</span>
									<span class="stat-label">avg</span>
								</div>
								<div class="stat">
									<span class="stat-value">{formatDuration(placeStats.longest_visit_min)}</span>
									<span class="stat-label">longest</span>
								</div>
								<div class="stat">
									<span class="stat-value">{formatDuration(placeStats.total_duration_min)}</span>
									<span class="stat-label">total</span>
								</div>
							</div>
							<div class="stats-dates">
								<span>First: {formatDate(placeStats.first_visit)}</span>
								<span>Last: {formatDate(placeStats.last_visit)}</span>
							</div>
						{:else}
							<div class="stats-empty">No visits recorded</div>
						{/if}
					</div>
				{/if}
			{/snippet}

			{#each groupedPlaces as [category, catPlaces] (category)}
				<CategoryGroup label={category} count={catPlaces.length} collapsible>
					{#each catPlaces as place (place.id)}
						<button
							class="place-btn"
							class:selected={selectedPlace?.id === place.id}
							onclick={() => handlePlaceClick(place)}
							type="button"
						>
							<span class="place-name">{place.name}</span>
						</button>
					{/each}
				</CategoryGroup>
			{/each}
		</Sidebar>
	{/snippet}

	{@render children()}
</AppShell>

{#if editingPlace}
	<PlaceForm
		place={editingPlace}
		onSave={handleEditSave}
		onDelete={handleDeletePlace}
		onCancel={() => (editingPlace = null)}
	/>
{/if}

<style>
	.stats-panel {
		border-bottom: 1px solid var(--border-subtle);
		padding: 0.6rem 0.75rem;
		flex-shrink: 0;
	}

	.stats-header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		margin-bottom: 0.5rem;
	}

	.stats-name {
		font-size: var(--text-sm);
		font-weight: 500;
	}

	.stats-actions {
		display: flex;
		gap: 0.15rem;
	}

	.stats-edit,
	.stats-close {
		background: none;
		border: none;
		color: var(--text-dim);
		font-size: var(--text-sm);
		cursor: pointer;
		padding: 0 0.25rem;
		line-height: 1;
	}

	.stats-edit:hover,
	.stats-close:hover {
		color: var(--text-muted);
	}

	.stats-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.4rem 0.75rem;
		margin-bottom: 0.5rem;
	}

	.stat {
		display: flex;
		flex-direction: column;
	}

	.stat-value {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-primary);
	}

	.stat-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.stats-dates {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.stats-loading,
	.stats-empty {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.stats-notes {
		font-size: var(--text-xs);
		color: var(--text-muted);
		font-style: italic;
		white-space: pre-wrap;
		padding: 0.25rem 0;
		border-top: 1px solid var(--border-subtle);
	}

	.place-btn {
		display: block;
		width: 100%;
		min-width: 0;
		max-width: 100%;
		background: none;
		border: none;
		color: inherit;
		font: inherit;
		font-size: var(--text-sm);
		line-height: 1.5;
		cursor: pointer;
		padding: 0.2rem 0.75rem;
		border-radius: 0.3rem;
		transition: background var(--transition-fast);
		text-align: left;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.place-btn:hover {
		background: var(--surface-raised);
	}

	.place-btn.selected {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.place-name {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
