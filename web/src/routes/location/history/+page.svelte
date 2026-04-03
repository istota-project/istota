<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import {
		getLocationPings,
		getDaySummary,
		getLocationPlaces,
		type LocationPing,
		type DaySummary,
		type DaySummaryStop,
		type Place,
	} from '$lib/api';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import StopTimeline from '$lib/components/location/StopTimeline.svelte';

	let pings: LocationPing[] = $state([]);
	let summary: DaySummary | null = $state(null);
	let places: Place[] = $state([]);
	let loading = $state(false);
	let error = $state('');
	let mapComponent: LocationMap | undefined = $state();

	let dateStr = $state('');
	let startStr = $state('');
	let endStr = $state('');
	let viewMode: 'day' | 'range' = $state('day');
	let showHeat = $state(false);

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

		try {
			if (viewMode === 'day' && dateStr) {
				const [p, s, pl] = await Promise.all([
					getLocationPings({ date: dateStr }),
					getDaySummary(dateStr),
					getLocationPlaces(),
				]);
				pings = p.pings;
				summary = s;
				places = pl.places;
			} else if (viewMode === 'range' && startStr && endStr) {
				const [p, pl] = await Promise.all([
					getLocationPings({ start: startStr, end: endStr, limit: '50000' }),
					getLocationPlaces(),
				]);
				pings = p.pings;
				places = pl.places;
				showHeat = true;
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

	onMount(() => {
		readUrlParams();
		loadData();
	});
</script>

<div class="location-page">
	<div class="page-header">
		<h1>Location</h1>
		<div class="nav-links">
			<a href="{base}/location">Today</a>
			<a href="{base}/location/history" class="active">History</a>
			<a href="{base}/location/places">Places</a>
		</div>
	</div>

	<div class="controls">
		<div class="presets">
			<button class:active={viewMode === 'day' && dateStr === today} onclick={() => selectDay(today)}>Today</button>
			<button class:active={viewMode === 'day' && dateStr === yesterday()} onclick={() => selectDay(yesterday())}>Yesterday</button>
			<button onclick={() => selectRange(thisWeekStart(), today)}>This week</button>
			<button onclick={() => selectRange(thisMonthStart(), today)}>This month</button>
		</div>
		<div class="date-inputs">
			<div class="input-group">
				<label for="date-input">Date</label>
				<input id="date-input" type="date" bind:value={dateStr} onchange={handleDateInput} max={today} />
			</div>
			<span class="separator">or</span>
			<div class="input-group">
				<label for="start-input">From</label>
				<input id="start-input" type="date" bind:value={startStr} onchange={handleRangeInput} max={today} />
			</div>
			<div class="input-group">
				<label for="end-input">To</label>
				<input id="end-input" type="date" bind:value={endStr} onchange={handleRangeInput} max={today} />
			</div>
		</div>
		{#if viewMode === 'range' && pings.length > 0}
			<label class="heat-toggle">
				<input type="checkbox" bind:checked={showHeat} />
				Heat map
			</label>
		{/if}
	</div>

	{#if loading}
		<div class="loading">Loading...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else if pings.length === 0}
		<div class="loading">No location data for this period</div>
	{:else}
		<div class="layout">
			<div class="map-panel">
				<LocationMap
					bind:this={mapComponent}
					{pings}
					stops={summary?.stops ?? []}
					{places}
					showPath={!showHeat}
					{showHeat}
					onStopClick={handleStopClick}
				/>
			</div>
			<div class="sidebar">
				<div class="stats">
					<div class="stat">
						<span class="stat-value">{pings.length}</span>
						<span class="stat-label">pings</span>
					</div>
					{#if summary}
						<div class="stat">
							<span class="stat-value">{summary.stops.length}</span>
							<span class="stat-label">stops</span>
						</div>
						<div class="stat">
							<span class="stat-value">{summary.transit_pings}</span>
							<span class="stat-label">transit</span>
						</div>
					{/if}
					{#if viewMode === 'range'}
						{@const uniquePlaces = new Set(pings.filter(p => p.place).map(p => p.place))}
						<div class="stat">
							<span class="stat-value">{uniquePlaces.size}</span>
							<span class="stat-label">places</span>
						</div>
					{/if}
				</div>
				{#if summary && summary.stops.length > 0}
					<div class="sidebar-section">
						<div class="section-label">Stops</div>
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

	.nav-links a:hover { color: var(--text-primary); }
	.nav-links a.active {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.controls {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 1rem;
		margin-bottom: 1rem;
	}

	.presets {
		display: flex;
		gap: 0.35rem;
	}

	.presets button {
		background: var(--surface-card);
		border: none;
		color: var(--text-muted);
		font-size: var(--text-sm);
		padding: 0.3rem 0.65rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
		transition: all var(--transition-fast);
		font-family: inherit;
	}

	.presets button:hover { color: var(--text-primary); background: var(--surface-raised); }
	.presets button.active { background: var(--surface-raised); color: var(--text-primary); }

	.date-inputs {
		display: flex;
		align-items: end;
		gap: 0.5rem;
	}

	.separator {
		font-size: var(--text-xs);
		color: var(--text-dim);
		padding-bottom: 0.3rem;
	}

	.input-group {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.input-group label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.input-group input[type="date"] {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font-size: var(--text-sm);
		padding: 0.3rem 0.5rem;
		border-radius: 0.3rem;
		font-family: inherit;
	}

	.input-group input[type="date"]::-webkit-calendar-picker-indicator {
		filter: invert(0.7);
	}

	.heat-toggle {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		font-size: var(--text-sm);
		color: var(--text-muted);
		cursor: pointer;
	}

	.heat-toggle input {
		accent-color: var(--text-primary);
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

	.stats {
		display: flex;
		gap: 1rem;
		flex-wrap: wrap;
	}

	.stat {
		display: flex;
		flex-direction: column;
		align-items: center;
	}

	.stat-value {
		font-size: 1.1rem;
		font-weight: 600;
	}

	.stat-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
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
	}

	@media (max-width: 768px) {
		.layout { grid-template-columns: 1fr; }
		.map-panel { height: 50vh; min-height: 300px; }
		.date-inputs { flex-wrap: wrap; }
	}
</style>
