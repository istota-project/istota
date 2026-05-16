<script lang="ts">
	import { onMount, untrack } from 'svelte';
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
		createHealthStat,
		deleteHealthStat,
		getHealthSettings,
		healthStatsSeries,
		listHealthStats,
		type HealthSettings,
		type HealthStat,
	} from '$lib/api';
	import {
		LOG_UNIT_CHOICES,
		METRIC_LABELS,
		METRIC_UNITS,
		formatStat,
		metricLabel,
		toCanonical,
	} from '$lib/health/units';

	Chart.register(LineController, LineElement, PointElement, CategoryScale, LinearScale, Tooltip, Filler);

	const METRIC_KEYS = Object.keys(METRIC_LABELS);

	type Range = '30d' | '90d' | '1y' | 'all';

	let range = $state<Range>('90d');
	let loading = $state(true);
	let error = $state('');
	let settings: HealthSettings | null = $state(null);

	// metric -> series points
	let seriesByMetric: Record<string, { measured_at: string; value: number; unit: string }[]> =
		$state({});
	// metric -> latest HealthStat (used for the headline value)
	let latestByMetric: Record<string, HealthStat> = $state({});

	// Per-card chart instances + canvas refs so we can rebuild on resize.
	const charts: Record<string, Chart | undefined> = {};
	const canvases: Record<string, HTMLCanvasElement | undefined> = $state({});

	// Modal state for manual entry.
	let modalOpen = $state(false);
	let formMetric = $state('weight');
	let formValue = $state('');
	let formUnit = $state('');
	let formDate = $state('');
	let formNotes = $state('');
	let saving = $state(false);
	let formError = $state('');

	function defaultUnitFor(metric: string): string {
		const choices = LOG_UNIT_CHOICES[metric];
		if (!choices) return METRIC_UNITS[metric] || '';
		const display = settings?.display_units;
		if (metric === 'weight' && display?.weight === 'lb') return 'lb';
		if (metric === 'body_temp' && display?.temp === 'F') return '°F';
		return choices[0];
	}

	// Reset formUnit to a sensible default whenever the chosen metric changes.
	$effect(() => {
		formMetric;
		untrack(() => {
			formUnit = defaultUnitFor(formMetric);
		});
	});

	function rangeSince(r: Range): string | undefined {
		if (r === 'all') return undefined;
		const days = { '30d': 30, '90d': 90, '1y': 365 }[r];
		return new Date(Date.now() - days * 86400 * 1000).toISOString();
	}

	async function load() {
		loading = true;
		error = '';
		try {
			const since = rangeSince(range);
			const [latest, ss] = await Promise.all([
				listHealthStats({ limit: 1000 }).then((r) => r.stats),
				settings ? Promise.resolve({ settings }) : getHealthSettings(),
			]);
			settings = ss.settings;
			// Determine which metrics have any data.
			const seen = new Set(latest.map((s) => s.metric));
			const series: Record<string, { measured_at: string; value: number; unit: string }[]> = {};
			const latestMap: Record<string, HealthStat> = {};
			for (const s of latest) {
				const prev = latestMap[s.metric];
				if (!prev || s.measured_at > prev.measured_at) latestMap[s.metric] = s;
			}
			latestByMetric = latestMap;
			// Fetch series for each present metric in parallel.
			const results = await Promise.all(
				[...seen].map(async (m) => {
					const resp = await healthStatsSeries(m, since ? { since } : {});
					return [m, resp.points] as const;
				}),
			);
			for (const [m, points] of results) {
				series[m] = points;
			}
			seriesByMetric = series;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load stats';
		} finally {
			loading = false;
		}
	}

	function renderChart(metric: string) {
		const canvas = canvases[metric];
		if (!canvas) return;
		const display = settings?.display_units ?? { weight: 'kg' as const, height: 'cm' as const, temp: 'C' as const };
		const points = seriesByMetric[metric] || [];
		const labels: string[] = [];
		const values: number[] = [];
		for (const p of points) {
			labels.push(new Date(p.measured_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }));
			values.push(formatStat(metric, p.value, p.unit, display).value);
		}
		if (charts[metric]) {
			charts[metric]!.destroy();
		}
		charts[metric] = new Chart(canvas, {
			type: 'line',
			data: {
				labels,
				datasets: [
					{
						data: values,
						borderColor: 'rgb(122, 163, 216)',
						backgroundColor: 'rgba(122, 163, 216, 0.15)',
						borderWidth: 1.5,
						tension: 0.25,
						fill: true,
						pointRadius: 0,
						pointHoverRadius: 3,
					},
				],
			},
			options: {
				responsive: true,
				maintainAspectRatio: false,
				plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
				scales: {
					x: { display: false },
					y: {
						display: true,
						grid: { color: 'rgba(255,255,255,0.04)' },
						ticks: {
							font: { size: 9 },
							maxTicksLimit: 4,
							color: 'rgba(255,255,255,0.4)',
						},
					},
				},
			},
		});
	}

	function renderBpChart() {
		const canvas = canvases['blood_pressure'];
		if (!canvas) return;
		const sys = seriesByMetric.blood_pressure_systolic || [];
		const dia = seriesByMetric.blood_pressure_diastolic || [];
		// Align points by date for a paired chart.
		const dates = new Set([...sys.map((p) => p.measured_at), ...dia.map((p) => p.measured_at)]);
		const sorted = [...dates].sort();
		const sysMap = new Map(sys.map((p) => [p.measured_at, p.value]));
		const diaMap = new Map(dia.map((p) => [p.measured_at, p.value]));
		const labels = sorted.map((d) =>
			new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
		);
		const sysValues = sorted.map((d) => sysMap.get(d) ?? null);
		const diaValues = sorted.map((d) => diaMap.get(d) ?? null);
		if (charts.blood_pressure) charts.blood_pressure!.destroy();
		charts.blood_pressure = new Chart(canvas, {
			type: 'line',
			data: {
				labels,
				datasets: [
					{
						label: 'Systolic',
						data: sysValues,
						borderColor: '#f08c8c',
						backgroundColor: 'transparent',
						borderWidth: 1.5,
						tension: 0.25,
						pointRadius: 0,
						pointHoverRadius: 3,
						spanGaps: true,
					},
					{
						label: 'Diastolic',
						data: diaValues,
						borderColor: '#7aa3d8',
						backgroundColor: 'transparent',
						borderWidth: 1.5,
						tension: 0.25,
						pointRadius: 0,
						pointHoverRadius: 3,
						spanGaps: true,
					},
				],
			},
			options: {
				responsive: true,
				maintainAspectRatio: false,
				plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
				scales: {
					x: { display: false },
					y: {
						display: true,
						grid: { color: 'rgba(255,255,255,0.04)' },
						ticks: { font: { size: 9 }, maxTicksLimit: 4, color: 'rgba(255,255,255,0.4)' },
					},
				},
			},
		});
	}

	$effect(() => {
		range;
		untrack(load);
	});

	$effect(() => {
		seriesByMetric;
		untrack(() => {
			// Render after the DOM updates with the new card list.
			queueMicrotask(() => {
				for (const m of Object.keys(seriesByMetric)) {
					if (m === 'blood_pressure_systolic' || m === 'blood_pressure_diastolic') continue;
					renderChart(m);
				}
				if (canvases['blood_pressure']) renderBpChart();
			});
		});
	});

	async function submitEntry(e: Event) {
		e.preventDefault();
		if (!formValue.trim()) return;
		saving = true;
		formError = '';
		try {
			const canonical = toCanonical(formMetric, Number(formValue), formUnit);
			await createHealthStat({
				metric: formMetric,
				value: canonical.value,
				unit: canonical.unit,
				measured_at: formDate || undefined,
				notes: formNotes.trim() || undefined,
			});
			modalOpen = false;
			formValue = '';
			formNotes = '';
			formDate = '';
			formUnit = defaultUnitFor(formMetric);
			await load();
		} catch (e) {
			formError = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}

	function openEntry(metric?: string) {
		if (metric) formMetric = metric;
		formUnit = defaultUnitFor(formMetric);
		modalOpen = true;
	}

	function formatLatestValue(metric: string): { value: number; unit: string } | null {
		const stat = latestByMetric[metric];
		if (!stat || !settings) return null;
		return formatStat(metric, stat.value, stat.unit, settings.display_units);
	}

	function bpHeadline(): string | null {
		const s = latestByMetric.blood_pressure_systolic;
		const d = latestByMetric.blood_pressure_diastolic;
		if (!s && !d) return null;
		return `${s ? Math.round(s.value) : '—'}/${d ? Math.round(d.value) : '—'}`;
	}

	function bpLatestDate(): string | null {
		const s = latestByMetric.blood_pressure_systolic;
		const d = latestByMetric.blood_pressure_diastolic;
		const iso = (s?.measured_at && d?.measured_at)
			? (s.measured_at > d.measured_at ? s.measured_at : d.measured_at)
			: (s?.measured_at || d?.measured_at);
		if (!iso) return null;
		return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function metricsToShow(): string[] {
		// Every metric with at least one data point, excluding the BP halves
		// (they merge into the combined card).
		return Object.keys(seriesByMetric).filter(
			(m) => m !== 'blood_pressure_systolic' && m !== 'blood_pressure_diastolic',
		).sort((a, b) => (METRIC_LABELS[a] || a).localeCompare(METRIC_LABELS[b] || b));
	}

	function hasBp(): boolean {
		return Boolean(
			seriesByMetric.blood_pressure_systolic?.length
				|| seriesByMetric.blood_pressure_diastolic?.length,
		);
	}

	function bmi(): number | null {
		const w = latestByMetric.weight;
		if (!w || !settings?.height_cm) return null;
		const h = settings.height_cm / 100;
		return Math.round((w.value / (h * h)) * 10) / 10;
	}

	onMount(load);
</script>

<div class="bar">
	<div class="ranges">
		{#each ['30d', '90d', '1y', 'all'] as r}
			<button
				class:active={range === r}
				onclick={() => (range = r as Range)}
				type="button"
			>{r}</button>
		{/each}
	</div>
	<button class="log-btn" onclick={() => openEntry()} type="button">+ Log measurement</button>
</div>

{#if loading}
	<div class="loading">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else if metricsToShow().length === 0 && !hasBp()}
	<div class="empty">
		No measurements yet.
		<button class="link" onclick={() => openEntry()} type="button">Log your first measurement</button>.
	</div>
{:else}
	<div class="grid">
		{#each metricsToShow() as metric (metric)}
			{@const v = formatLatestValue(metric)}
			<button class="card" onclick={() => openEntry(metric)} type="button">
				<header>
					<span class="label">{metricLabel(metric)}</span>
					<span class="count">{(seriesByMetric[metric] || []).length} pts</span>
				</header>
				<div class="value">
					{#if v}
						{v.value}<span class="unit">{v.unit}</span>
					{:else}
						—
					{/if}
				</div>
				<div class="chart">
					<canvas bind:this={canvases[metric]}></canvas>
				</div>
				{#if metric === 'weight' && bmi() != null}
					<div class="meta">BMI {bmi()}</div>
				{:else if latestByMetric[metric]}
					<div class="meta">
						{new Date(latestByMetric[metric].measured_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
					</div>
				{/if}
			</button>
		{/each}
		{#if hasBp()}
			<button class="card" onclick={() => openEntry('blood_pressure_systolic')} type="button">
				<header>
					<span class="label">Blood Pressure</span>
					<span class="count">
						{Math.max(
							(seriesByMetric.blood_pressure_systolic || []).length,
							(seriesByMetric.blood_pressure_diastolic || []).length,
						)} pts
					</span>
				</header>
				<div class="value">
					{bpHeadline() || '—'}<span class="unit">mmHg</span>
				</div>
				<div class="chart">
					<canvas bind:this={canvases['blood_pressure']}></canvas>
				</div>
				{#if bpLatestDate()}<div class="meta">{bpLatestDate()}</div>{/if}
			</button>
		{/if}
	</div>
{/if}

{#if modalOpen}
	<div class="modal-backdrop" onclick={() => (modalOpen = false)} role="presentation">
		<div class="modal" onclick={(e) => e.stopPropagation()} role="presentation">
			<h2>Log measurement</h2>
			<form onsubmit={submitEntry}>
				<label>
					<span>Metric</span>
					<select bind:value={formMetric}>
						{#each METRIC_KEYS as m}<option value={m}>{METRIC_LABELS[m]}</option>{/each}
					</select>
				</label>
				<label>
					<span>Value</span>
					<div class="value-row">
						<input type="number" step="any" bind:value={formValue} required />
						{#if LOG_UNIT_CHOICES[formMetric]}
							<select class="unit-select" bind:value={formUnit}>
								{#each LOG_UNIT_CHOICES[formMetric] as u}
									<option value={u}>{u}</option>
								{/each}
							</select>
						{:else}
							<span class="unit-static">{METRIC_UNITS[formMetric]}</span>
						{/if}
					</div>
				</label>
				<label>
					<span>When</span>
					<input type="datetime-local" bind:value={formDate} />
				</label>
				<label>
					<span>Notes</span>
					<input type="text" bind:value={formNotes} placeholder="optional" />
				</label>
				{#if formError}<div class="msg error inline">{formError}</div>{/if}
				<div class="modal-actions">
					<button class="btn" type="button" onclick={() => (modalOpen = false)} disabled={saving}>Cancel</button>
					<button class="btn primary" type="submit" disabled={saving}>
						{saving ? 'Saving…' : 'Save'}
					</button>
				</div>
			</form>
		</div>
	</div>
{/if}

<style>
	.bar {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 1rem;
	}
	.ranges {
		display: flex;
		gap: 0.3rem;
	}
	.ranges button {
		background: none;
		border: 1px solid var(--border-default);
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.25rem 0.6rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}
	.ranges button.active {
		color: var(--text-primary);
		border-color: var(--text-primary);
	}
	.log-btn {
		padding: 0.35rem 0.85rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		cursor: pointer;
	}
	.log-btn:hover {
		background: var(--surface-raised);
	}
	.empty { color: var(--text-dim); padding: 2rem 0; }
	.link {
		background: none;
		border: none;
		color: var(--text-primary);
		font: inherit;
		cursor: pointer;
		text-decoration: underline;
		padding: 0;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
		gap: 0.75rem;
	}
	.card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.75rem 0.9rem;
		text-align: left;
		font: inherit;
		color: var(--text-primary);
		cursor: pointer;
		display: flex;
		flex-direction: column;
		min-height: 170px;
	}
	.card:hover {
		border-color: #555;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
	}
	.label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.count {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}
	.value {
		font-size: 1.6rem;
		font-weight: 500;
		margin-top: 0.25rem;
		line-height: 1.1;
	}
	.unit {
		font-size: var(--text-sm);
		color: var(--text-muted);
		margin-left: 0.25rem;
	}
	.chart {
		flex: 1;
		min-height: 70px;
		margin-top: 0.4rem;
	}
	.meta {
		font-size: var(--text-xs);
		color: var(--text-dim);
		margin-top: 0.25rem;
	}
	.msg {
		font-size: var(--text-sm);
		padding: 0.4rem 0.6rem;
		border-radius: 0.3rem;
	}
	.msg.error { background: rgba(204, 102, 102, 0.1); color: #f0a; }
	.msg.inline { margin: 0.25rem 0; }

	.modal-backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.7);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 100;
	}
	.modal {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 1.5rem;
		width: 26rem;
		max-width: 90vw;
	}
	.modal h2 {
		font-size: var(--text-base);
		font-weight: 500;
		margin: 0 0 1rem;
	}
	.modal form {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.modal label {
		display: grid;
		grid-template-columns: 6rem 1fr;
		gap: 0.5rem;
		align-items: center;
		font-size: var(--text-sm);
	}
	.modal label > span {
		color: var(--text-muted);
		font-size: var(--text-xs);
	}
	.value-row {
		display: flex;
		gap: 0.4rem;
		align-items: center;
	}
	.value-row input {
		flex: 1;
	}
	.unit-select {
		width: auto;
		min-width: 4.5rem;
		flex: 0 0 auto;
	}
	.unit-static {
		font-size: var(--text-sm);
		color: var(--text-muted);
		padding: 0.3rem 0.2rem;
	}
	.modal input, .modal select {
		background: var(--surface-raised);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.3rem 0.5rem;
		width: 100%;
		box-sizing: border-box;
	}
	.modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 0.5rem;
	}
	.btn {
		padding: 0.4rem 0.85rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		cursor: pointer;
	}
	.btn:hover:not(:disabled) { background: var(--surface-raised); }
	.btn:disabled { opacity: 0.6; cursor: not-allowed; }
	.btn.primary { border-color: #7aa3d8; color: #7aa3d8; }
</style>
