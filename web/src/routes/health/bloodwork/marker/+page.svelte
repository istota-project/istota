<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import {
		Chart,
		LineController,
		LineElement,
		PointElement,
		CategoryScale,
		LinearScale,
		Tooltip,
		Filler,
	} from 'chart.js';
	import {
		getBiomarkerExplainer,
		healthBiomarkerRefs,
		healthBiomarkerTrend,
		type BiomarkerExplainer,
		type BiomarkerRef,
		type BiomarkerTrend,
	} from '$lib/api';

	Chart.register(LineController, LineElement, PointElement, CategoryScale, LinearScale, Tooltip, Filler);

	// Read the canonical biomarker name from ?name=… so this page is
	// statically prerenderable under adapter-static.
	let name = $derived(page.url.searchParams.get('name') ?? '');

	let loading = $state(true);
	let error = $state('');
	let trend: BiomarkerTrend | null = $state(null);
	let ref: BiomarkerRef | null = $state(null);
	let related: BiomarkerRef[] = $state([]);

	let chartCanvas: HTMLCanvasElement | undefined = $state();
	let chart: Chart | undefined;

	let explainer: BiomarkerExplainer | null = $state(null);
	let explainerLoading = $state(false);
	let explainerError = $state('');

	function latestDirection(): 'high' | 'low' | null {
		if (!trend || trend.points.length === 0) return null;
		const latest = trend.points[trend.points.length - 1];
		if (latest.flag === 'H' || latest.flag === 'C') return 'high';
		if (latest.flag === 'L') return 'low';
		// Fallback when the panel didn't carry a flag — compare against
		// the canonical range we already loaded.
		if (trend.ref_range_high != null && latest.value > trend.ref_range_high) return 'high';
		if (trend.ref_range_low != null && latest.value < trend.ref_range_low) return 'low';
		return null;
	}

	async function loadExplainer() {
		const direction = latestDirection();
		if (!direction || !trend) {
			explainer = null;
			return;
		}
		explainerLoading = true;
		explainerError = '';
		try {
			explainer = await getBiomarkerExplainer(trend.name, direction);
		} catch (e) {
			explainerError = e instanceof Error ? e.message : 'Failed to load alert';
			explainer = null;
		} finally {
			explainerLoading = false;
		}
	}

	async function load() {
		loading = true;
		error = '';
		try {
			const [t, refsResp] = await Promise.all([
				healthBiomarkerTrend(name),
				healthBiomarkerRefs(),
			]);
			trend = t;
			ref = refsResp.refs.find((r) => r.name === name) || null;
			if (ref) {
				related = refsResp.refs.filter(
					(r) => r.category === ref!.category && r.name !== ref!.name,
				);
			} else {
				related = [];
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load biomarker history';
		} finally {
			loading = false;
		}
	}

	function renderChart() {
		if (!chartCanvas || !trend) return;
		if (chart) {
			chart.destroy();
			chart = undefined;
		}
		const labels = trend.points.map((p) =>
			new Date(p.drawn_at + (p.drawn_at.includes('T') ? '' : 'T00:00:00Z'))
				.toLocaleDateString(undefined, { year: 'numeric', month: 'short' }),
		);
		const values = trend.points.map((p) => p.value);
		const high = trend.ref_range_high;
		const low = trend.ref_range_low;
		const OUT_OF_RANGE = 'rgba(204, 102, 102, 0.22)';

		const datasets: any[] = [];

		// Out-of-range fill: an invisible duplicate of the line, with a
		// fill anchored to the high threshold. Chart.js paints the region
		// between the line and y=high in `above` when the line is above
		// it, and `below` otherwise — so only the over-the-bound area
		// gets the red wash.
		if (high != null) {
			datasets.push({
				label: '_high_band',
				data: values,
				borderColor: 'transparent',
				borderWidth: 0,
				pointRadius: 0,
				tension: 0.25,
				fill: {
					target: { value: high },
					above: OUT_OF_RANGE,
					below: 'transparent',
				},
			});
		}
		// Same trick on the low side — the region between the line and
		// y=low is filled red when the line dips below it.
		if (low != null) {
			datasets.push({
				label: '_low_band',
				data: values,
				borderColor: 'transparent',
				borderWidth: 0,
				pointRadius: 0,
				tension: 0.25,
				fill: {
					target: { value: low },
					above: 'transparent',
					below: OUT_OF_RANGE,
				},
			});
		}

		// Main visible line — sits on top of the fills.
		datasets.push({
			label: trend.display_name || trend.name,
			data: values,
			borderColor: '#7aa3d8',
			backgroundColor: 'transparent',
			borderWidth: 2,
			tension: 0.25,
			fill: false,
			pointRadius: 3,
			pointHoverRadius: 6,
		});

		// Dashed threshold markers so the boundary is visible even when
		// the line stays well clear of it.
		if (low != null) {
			datasets.push({
				label: '_low_marker',
				data: values.map(() => low),
				borderColor: 'rgba(204, 102, 102, 0.5)',
				borderDash: [4, 4],
				borderWidth: 1,
				pointRadius: 0,
				fill: false,
				tension: 0,
			});
		}
		if (high != null) {
			datasets.push({
				label: '_high_marker',
				data: values.map(() => high),
				borderColor: 'rgba(204, 102, 102, 0.5)',
				borderDash: [4, 4],
				borderWidth: 1,
				pointRadius: 0,
				fill: false,
				tension: 0,
			});
		}

		chart = new Chart(chartCanvas, {
			type: 'line',
			data: { labels, datasets },
			options: {
				responsive: true,
				maintainAspectRatio: false,
				plugins: {
					legend: { display: false },
					tooltip: {
						mode: 'index',
						intersect: false,
						// Hide the helper datasets from the tooltip; only the
						// real line carries meaningful values.
						filter: (item) => !String(item.dataset.label || '').startsWith('_'),
					},
				},
				scales: {
					x: { grid: { color: 'rgba(255,255,255,0.04)' } },
					y: {
						grid: { color: 'rgba(255,255,255,0.04)' },
						beginAtZero: false,
					},
				},
			},
		});
	}

	$effect(() => {
		name;
		untrack(load);
	});

	$effect(() => {
		trend;
		untrack(() => queueMicrotask(renderChart));
	});

	$effect(() => {
		trend;
		untrack(loadExplainer);
	});

	function formatDate(iso: string): string {
		try {
			const d = new Date(iso + (iso.includes('T') ? '' : 'T00:00:00Z'));
			return d.toLocaleDateString(undefined, {
				year: 'numeric', month: 'short', day: 'numeric',
			});
		} catch {
			return iso;
		}
	}

	function formatRange(low: number | null, high: number | null): string {
		if (low == null && high == null) return '—';
		if (low == null) return `≤ ${high}`;
		if (high == null) return `≥ ${low}`;
		return `${low} – ${high}`;
	}

	function encodeMarker(n: string): string {
		return encodeURIComponent(n);
	}

	function trendSummary(): string {
		if (!trend || trend.points.length < 2) return '';
		const first = trend.points[0].value;
		const last = trend.points[trend.points.length - 1].value;
		const diff = last - first;
		const pct = first !== 0 ? (diff / first) * 100 : 0;
		const sign = diff > 0 ? '+' : '';
		const dir = Math.abs(pct) < 1 ? 'flat' : pct > 0 ? 'up' : 'down';
		return `${dir} · ${sign}${Math.round(pct * 10) / 10}% over ${trend.points.length} measurements`;
	}

	onMount(load);
</script>

<a class="back" href="{base}/health/bloodwork">← Bloodwork</a>

{#if loading}
	<div class="empty">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else if trend}
	<header class="page-header">
		<div>
			<h1>{ref?.display_name || trend.name}</h1>
			<div class="meta">
				{#if ref}<span class="cat">{ref.category}</span>{/if}
				{#if trend.unit}<span class="unit">{trend.unit}</span>{/if}
				{#if trend.points.length > 0}
					<span class="summary">{trendSummary()}</span>
				{/if}
			</div>
		</div>
	</header>

	{#if related.length > 0}
		<div class="related">
			<span class="related-label">Related:</span>
			{#each related as r (r.name)}
				<a href="{base}/health/bloodwork/marker?name={encodeMarker(r.name)}">{r.display_name}</a>
			{/each}
		</div>
	{/if}

	{#if latestDirection()}
		<section class="alert alert-{latestDirection()}">
			<header>
				<span class="alert-pill">{latestDirection() === 'high' ? 'ABOVE RANGE' : 'BELOW RANGE'}</span>
				<span class="alert-title">
					What a {latestDirection()} {ref?.display_name || trend.name} reading can indicate
				</span>
				{#if explainer?.source === 'fallback'}
					<span class="alert-tag" title="LLM unavailable — generic copy">generic</span>
				{:else if explainer?.source === 'generated'}
					<span class="alert-tag" title="Freshly generated">new</span>
				{/if}
			</header>
			{#if explainerLoading}
				<div class="loading">Generating context…</div>
			{:else if explainerError}
				<div class="error-text">{explainerError}</div>
			{:else if explainer}
				<p class="summary">{explainer.summary}</p>
				{#if explainer.causes.length > 0}
					<div class="block">
						<h3>What might contribute</h3>
						<ul>
							{#each explainer.causes as c}<li>{c}</li>{/each}
						</ul>
					</div>
				{/if}
				{#if explainer.mitigations.length > 0}
					<div class="block">
						<h3>Things to consider</h3>
						<ul>
							{#each explainer.mitigations as m}<li>{m}</li>{/each}
						</ul>
					</div>
				{/if}
				<footer class="disclaimer">{explainer.disclaimer}</footer>
			{/if}
		</section>
	{/if}

	<section class="chart-card">
		{#if trend.points.length === 0}
			<div class="empty">No measurements recorded for this biomarker yet.</div>
		{:else}
			{#if trend.unit_mismatch}
				<div class="msg warn">
					Measurements use different units across panels — the chart shows raw values without conversion.
				</div>
			{/if}
			<div class="chart-wrap"><canvas bind:this={chartCanvas}></canvas></div>
		{/if}
	</section>

	{#if ref?.description}
		<section class="about">
			<h2>About this marker</h2>
			<p>{ref.description}</p>
			<dl>
				<dt>Reference range</dt>
				<dd>
					{formatRange(trend.ref_range_low, trend.ref_range_high)}
					{#if trend.unit}{trend.unit}{/if}
				</dd>
				{#if ref.aliases.length > 0}
					<dt>Also called</dt>
					<dd>{ref.aliases.join(', ')}</dd>
				{/if}
				<dt>Category</dt>
				<dd>{ref.category}</dd>
			</dl>
		</section>
	{/if}

	{#if trend.points.length > 0}
		<section class="history">
			<h2>History</h2>
			<table>
				<thead>
					<tr>
						<th>Date</th>
						<th>Value</th>
						<th>Unit</th>
						<th>Flag</th>
					</tr>
				</thead>
				<tbody>
					{#each [...trend.points].reverse() as p}
						<tr class:flag-row={p.flag}>
							<td>{formatDate(p.drawn_at)}</td>
							<td>{p.value}</td>
							<td>{p.unit}</td>
							<td>
								{#if p.flag}<span class="flag flag-{p.flag}">{p.flag}</span>{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</section>
	{/if}
{/if}

<style>
	.back {
		font-size: var(--text-xs);
		color: var(--text-muted);
		text-decoration: none;
	}
	.page-header {
		margin: 0.25rem 0 1rem;
	}
	h1 {
		font-size: var(--text-xl);
		font-weight: 500;
		margin: 0;
	}
	.meta {
		display: flex;
		gap: 0.75rem;
		align-items: baseline;
		margin-top: 0.25rem;
		font-size: var(--text-sm);
		color: var(--text-muted);
	}
	.cat {
		padding: 0 0.5rem;
		background: var(--surface-raised);
		border-radius: var(--radius-pill);
		font-size: var(--text-xs);
		color: var(--text-muted);
	}
	.unit {
		color: var(--text-dim);
	}
	.summary {
		color: var(--text-dim);
		font-size: var(--text-xs);
	}
	.related {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
		align-items: baseline;
		margin-bottom: 1rem;
	}
	.related-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}
	.related a {
		font-size: var(--text-xs);
		color: var(--text-muted);
		padding: 0.15rem 0.55rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		text-decoration: none;
	}
	.related a:hover {
		color: var(--text-primary);
		border-color: #555;
	}
	.alert {
		background: rgba(204, 102, 102, 0.08);
		border: 1px solid rgba(204, 102, 102, 0.35);
		border-radius: var(--radius-card);
		padding: 1rem 1.25rem;
		margin-bottom: 1rem;
	}
	.alert-low {
		background: rgba(122, 163, 216, 0.08);
		border-color: rgba(122, 163, 216, 0.35);
	}
	.alert header {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		margin-bottom: 0.6rem;
		flex-wrap: wrap;
	}
	.alert-pill {
		font-size: var(--text-xs);
		font-weight: 600;
		padding: 0.15rem 0.55rem;
		border-radius: var(--radius-pill);
		background: rgba(204, 102, 102, 0.25);
		color: #f8a09c;
		letter-spacing: 0.05em;
	}
	.alert-low .alert-pill {
		background: rgba(122, 163, 216, 0.25);
		color: #9cc7f8;
	}
	.alert-title {
		font-size: var(--text-base);
		font-weight: 500;
		color: var(--text-primary);
	}
	.alert-tag {
		font-size: 10px;
		padding: 0.05rem 0.4rem;
		border-radius: var(--radius-pill);
		background: var(--surface-raised);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}
	.alert .summary {
		font-size: var(--text-sm);
		line-height: 1.55;
		color: var(--text-primary);
		margin: 0 0 0.85rem;
	}
	.alert .block {
		margin-bottom: 0.75rem;
	}
	.alert .block h3 {
		font-size: var(--text-xs);
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		margin: 0 0 0.35rem;
	}
	.alert ul {
		margin: 0;
		padding-left: 1.1rem;
		color: var(--text-primary);
		font-size: var(--text-sm);
		line-height: 1.5;
	}
	.alert ul li {
		margin-bottom: 0.2rem;
	}
	.alert .disclaimer {
		margin-top: 0.6rem;
		padding-top: 0.6rem;
		border-top: 1px solid rgba(255, 255, 255, 0.08);
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-style: italic;
	}
	.alert .loading {
		color: var(--text-muted);
		font-size: var(--text-sm);
		font-style: italic;
	}
	.alert .error-text {
		color: #f08c8c;
		font-size: var(--text-sm);
	}

	.chart-card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 1rem;
		margin-bottom: 1rem;
	}
	.chart-wrap {
		height: 340px;
	}
	.about {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 1rem;
		margin-bottom: 1rem;
	}
	.about h2, .history h2 {
		font-size: var(--text-base);
		font-weight: 500;
		margin: 0 0 0.5rem;
	}
	.about p {
		font-size: var(--text-sm);
		color: var(--text-muted);
		line-height: 1.55;
		margin: 0 0 0.75rem;
	}
	.about dl {
		display: grid;
		grid-template-columns: 9rem 1fr;
		gap: 0.3rem 0.75rem;
		font-size: var(--text-sm);
		margin: 0;
	}
	.about dt {
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-size: var(--text-xs);
	}
	.about dd {
		margin: 0;
		color: var(--text-primary);
	}
	.history {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 1rem;
	}
	.history table {
		width: 100%;
		border-collapse: collapse;
	}
	.history th, .history td {
		padding: 0.35rem 0.5rem;
		text-align: left;
		font-size: var(--text-sm);
		border-bottom: 1px solid var(--border-subtle);
	}
	.history th {
		color: var(--text-dim);
		font-weight: 400;
		text-transform: uppercase;
		font-size: var(--text-xs);
		letter-spacing: 0.04em;
	}
	.flag-row { background: rgba(204, 102, 102, 0.05); }
	.flag {
		display: inline-flex;
		justify-content: center;
		align-items: center;
		min-width: 1.5rem;
		padding: 0 0.4rem;
		border-radius: var(--radius-pill);
		font-size: var(--text-xs);
		font-weight: 500;
	}
	.flag-H { background: #4a2020; color: #f8a09c; }
	.flag-L { background: #20384a; color: #9cc7f8; }
	.flag-C { background: #6b0000; color: #fff; }
	.empty { color: var(--text-dim); padding: 1rem 0; }
	.msg {
		font-size: var(--text-sm);
		padding: 0.4rem 0.6rem;
		border-radius: 0.3rem;
	}
	.msg.error { background: rgba(204, 102, 102, 0.1); color: #f0a; }
	.msg.warn { background: rgba(230, 185, 107, 0.1); color: #e6b96b; margin-bottom: 0.75rem; }
</style>
