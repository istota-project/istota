<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { deletePlace, type Place } from '$lib/api';
	import { locationPlaces, reloadPlaces, mapFlyTo } from '$lib/stores/location';

	let { children } = $props();

	let places: Place[] = $state([]);
	let sidebarOpen = $state(false);

	locationPlaces.subscribe(v => places = v);

	function isActive(path: string): boolean {
		return page.url.pathname.startsWith(`${base}${path}`);
	}

	function isExactActive(path: string): boolean {
		const current = page.url.pathname;
		return current === `${base}${path}` || current === `${base}${path}/`;
	}

	function handlePlaceClick(place: Place) {
		const fly = $mapFlyTo;
		if (fly) fly(place.lat, place.lon, 15);
		sidebarOpen = false;
	}

	async function handleDeletePlace(place: Place) {
		try {
			await deletePlace(place.id);
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

	onMount(async () => {
		try {
			await reloadPlaces();
		} catch {
			// pages handle their own errors
		}
	});
</script>

<div class="loc-shell">
	<div class="loc-header">
		<h1>Location</h1>
		<div class="loc-nav">
			<a href="{base}/location" class:active={isExactActive('/location')}>Today</a>
			<a href="{base}/location/history" class:active={isActive('/location/history')}>History</a>
			<a href="{base}/location/places" class:active={isActive('/location/places')}>Places</a>
		</div>
		<button class="sidebar-toggle" onclick={() => sidebarOpen = !sidebarOpen} type="button">
			{sidebarOpen ? 'Close' : 'Places'} ({places.length})
		</button>
	</div>

	<div class="loc-body">
		<aside class="loc-sidebar" class:open={sidebarOpen}>
			<div class="sidebar-header">
				<span class="sidebar-title">Places</span>
				<span class="sidebar-count">{places.length}</span>
			</div>
			<div class="sidebar-list">
				{#each groupedPlaces as [category, catPlaces]}
					<div class="cat-group">
						<div class="cat-label">{category}</div>
						{#each catPlaces as place}
							<div class="place-row">
								<button
									class="place-btn"
									onclick={() => handlePlaceClick(place)}
									type="button"
								>
									<span class="place-name">{place.name}</span>
									<span class="place-radius">{place.radius_meters}m</span>
								</button>
								<button
									class="place-delete"
									onclick={() => handleDeletePlace(place)}
									type="button"
									title="Delete place"
								>&times;</button>
							</div>
						{/each}
					</div>
				{/each}
			</div>
		</aside>

		<div class="loc-main">
			{@render children()}
		</div>
	</div>
</div>

<style>
	.loc-shell {
		display: flex;
		flex-direction: column;
		/* Break out of .app-content padding to fill viewport */
		margin: -1.5rem;
		height: calc(100vh - 42px); /* 42px = app-nav height */
		overflow: hidden;
	}

	.loc-header {
		display: flex;
		align-items: baseline;
		gap: 1rem;
		padding: 0.75rem 1.5rem;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	.loc-header h1 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0;
	}

	.loc-nav {
		display: flex;
		gap: 0.35rem;
	}

	.loc-nav a {
		font-size: var(--text-sm);
		color: var(--text-muted);
		text-decoration: none;
		padding: 0.2rem 0.55rem;
		border-radius: var(--radius-pill);
		transition: all var(--transition-fast);
	}

	.loc-nav a:hover { color: var(--text-primary); }
	.loc-nav a.active {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.sidebar-toggle {
		display: none;
		margin-left: auto;
		background: var(--surface-card);
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.25rem 0.6rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}

	.loc-body {
		display: flex;
		flex: 1;
		min-height: 0;
	}

	.loc-sidebar {
		width: 200px;
		flex-shrink: 0;
		border-right: 1px solid var(--border-subtle);
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.sidebar-header {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		padding: 0.6rem 1rem 0.6rem 1.5rem;
		flex-shrink: 0;
	}

	.sidebar-title {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.sidebar-count {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.sidebar-list {
		flex: 1;
		overflow-y: auto;
		padding: 0 0.5rem 0.5rem;
	}

	.sidebar-list::-webkit-scrollbar {
		width: 4px;
	}

	.sidebar-list::-webkit-scrollbar-track {
		background: transparent;
	}

	.sidebar-list::-webkit-scrollbar-thumb {
		background: var(--border-default);
		border-radius: 2px;
	}

	.cat-group {
		margin-bottom: 0.25rem;
	}

	.cat-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		padding: 0.35rem 1rem 0.15rem;
	}

	.place-row {
		display: flex;
		align-items: center;
	}

	.place-btn {
		display: flex;
		justify-content: space-between;
		align-items: center;
		flex: 1;
		min-width: 0;
		background: none;
		border: none;
		color: inherit;
		font: inherit;
		cursor: pointer;
		padding: 0.3rem 1rem;
		border-radius: 0.3rem;
		transition: background var(--transition-fast);
		text-align: left;
	}

	.place-btn:hover {
		background: var(--surface-raised);
	}

	.place-delete {
		background: none;
		border: none;
		color: var(--text-dim);
		font-size: var(--text-sm);
		cursor: pointer;
		padding: 0.2rem 0.35rem;
		border-radius: 0.2rem;
		opacity: 0;
		transition: opacity var(--transition-fast), color var(--transition-fast);
	}

	.place-row:hover .place-delete { opacity: 1; }
	.place-delete:hover { color: #c66; }

	.place-name {
		font-size: var(--text-sm);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.place-radius {
		font-size: var(--text-xs);
		color: var(--text-dim);
		flex-shrink: 0;
		margin-left: 0.25rem;
	}

	.loc-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
	}

	@media (max-width: 768px) {
		.loc-shell {
			margin: -1rem -0.75rem;
			height: calc(100vh - 36px);
		}

		.loc-header {
			padding: 0.5rem 0.75rem;
		}

		.sidebar-toggle {
			display: block;
		}

		.loc-sidebar {
			display: none;
			position: absolute;
			top: 0;
			left: 0;
			bottom: 0;
			z-index: 20;
			width: 220px;
			background: var(--surface-base);
			border-right: 1px solid var(--border-default);
		}

		.loc-sidebar.open {
			display: flex;
		}

		.loc-body {
			position: relative;
		}
	}
</style>
