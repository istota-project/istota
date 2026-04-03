<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { onMount, onDestroy } from 'svelte';
	import {
		getLocationPings,
		getDaySummary,
		type LocationPing,
		type DaySummary,
		type DaySummaryStop,
	} from '$lib/api';
	import { locationPlaces, mapFlyTo } from '$lib/stores/location';
	import LocationMap from '$lib/components/location/LocationMap.svelte';
	import StopTimeline from '$lib/components/location/StopTimeline.svelte';

	let pings: LocationPing[] = $state([]);
	let summary: DaySummary | null = $state(null);
	let loading = $state(false);
	let error = $state('');
	let mapComponent: LocationMap | undefined = $state();

	let dateStr = $state('');
	let startStr = $state('');
	let endStr = $state('');
	let viewMode: 'day' | 'range' = $state('day');
	let showHeat = $state(false);
	let panelOpen = $state(false);

	let places = $derived($locationPlaces);

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
				const [p, s] = await Promise.all([
					getLocationPings({ date: dateStr }),
					getDaySummary(dateStr),
				]);
				pings = p.pings;
				summary = s;
				panelOpen = s.stops.length > 0;
			} else if (viewMode === 'range' && startStr && endStr) {
				const p = await getLocationPings({ start: startStr, end: endStr, limit: '50000' });
				pings = p.pings;
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
		showHeat = false;
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
	<div class="controls-bar">
		<div class="presets">
			<button class:active={viewMode === 'day' && dateStr === today} onclick={() => selectDay(today)}>Today</button>
			<button class:active={viewMode === 'day' && dateStr === yesterday()} onclick={() => selectDay(yesterday())}>Yesterday</button>
			<button onclick={() => selectRange(thisWeekStart(), today)}>This week</button>
			<button onclick={() => selectRange(thisMonthStart(), today)}>This month</button>
		</div>
		<div class="date-inputs">
			<label for="hist-date">Date</label>
			<input id="hist-date" type="date" bind:value={dateStr} onchange={handleDateInput} max={today} />
			<span class="sep">or</span>
			<label for="hist-start">From</label>
			<input id="hist-start" type="date" bind:value={startStr} onchange={handleRangeInput} max={today} />
			<label for="hist-end">To</label>
			<input id="hist-end" type="date" bind:value={endStr} onchange={handleRangeInput} max={today} />
		</div>
		{#if viewMode === 'range' && pings.length > 0}
			<label class="heat-toggle">
				<input type="checkbox" bind:checked={showHeat} />
				Heat map
			</label>
		{/if}
	</div>

	{#if loading}
		<div class="center-msg">Loading...</div>
	{:else if error}
		<div class="center-msg error">{error}</div>
	{:else if pings.length === 0}
		<div class="center-msg">No location data for this period</div>
	{:else}
		<div class="map-area">
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

		<div class="stats-bar">
			<span class="stat">{pings.length} pings</span>
			{#if summary}
				<span class="stat">{summary.stops.length} stops</span>
				<span class="stat">{summary.transit_pings} transit</span>
			{/if}
			{#if viewMode === 'range'}
				{@const uniquePlaces = new Set(pings.filter(p => p.place).map(p => p.place))}
				<span class="stat">{uniquePlaces.size} places</span>
			{/if}
			{#if summary && summary.stops.length > 0}
				<button class="stops-btn" onclick={() => panelOpen = !panelOpen} type="button">
					{panelOpen ? 'Hide stops' : 'Show stops'}
				</button>
			{/if}
		</div>

		{#if panelOpen && summary && summary.stops.length > 0}
			<div class="stops-panel">
				<StopTimeline stops={summary.stops} onStopClick={handleStopClick} />
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

	.controls-bar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem;
		padding: 0.5rem 0.75rem;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	.presets {
		display: flex;
		gap: 0.25rem;
	}

	.presets button {
		background: var(--surface-card);
		border: none;
		color: var(--text-muted);
		font-size: var(--text-xs);
		padding: 0.25rem 0.5rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
		transition: all var(--transition-fast);
		font-family: inherit;
	}

	.presets button:hover { color: var(--text-primary); background: var(--surface-raised); }
	.presets button.active { background: var(--surface-raised); color: var(--text-primary); }

	.date-inputs {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}

	.date-inputs label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.sep {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.date-inputs input[type="date"] {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font-size: var(--text-xs);
		padding: 0.2rem 0.4rem;
		border-radius: 0.25rem;
		font-family: inherit;
	}

	.date-inputs input[type="date"]::-webkit-calendar-picker-indicator {
		filter: invert(0.7);
	}

	.heat-toggle {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		font-size: var(--text-xs);
		color: var(--text-muted);
		cursor: pointer;
	}

	.heat-toggle input { accent-color: var(--text-primary); }

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
	}

	.stat {
		font-size: var(--text-xs);
		color: var(--text-muted);
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

	@media (max-width: 768px) {
		.date-inputs { display: none; }
	}
</style>
