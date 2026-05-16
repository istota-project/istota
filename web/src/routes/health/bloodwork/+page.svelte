<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getBloodworkMatrix,
		healthCsvExportUrl,
		importHealthCsv,
		listHealthPanels,
		type BloodworkMatrix,
		type CsvImportSummary,
		type HealthPanel,
	} from '$lib/api';

	let loading = $state(true);
	let error = $state('');
	let matrix: BloodworkMatrix | null = $state(null);
	let drafts: HealthPanel[] = $state([]);

	let csvInput: HTMLInputElement | undefined = $state(undefined);
	let csvImporting = $state(false);
	let csvSummary: CsvImportSummary | null = $state(null);

	async function onCsvPicked(e: Event) {
		const input = e.target as HTMLInputElement;
		const f = input.files?.[0];
		if (!f) return;
		csvImporting = true;
		csvSummary = null;
		error = '';
		try {
			csvSummary = await importHealthCsv(f);
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'CSV import failed';
		} finally {
			csvImporting = false;
			if (csvInput) csvInput.value = '';
		}
	}

	function triggerCsvPick() {
		csvInput?.click();
	}

	async function load() {
		loading = true;
		error = '';
		try {
			const [m, panelResp] = await Promise.all([
				getBloodworkMatrix(),
				listHealthPanels({ limit: 200 }),
			]);
			matrix = m;
			drafts = panelResp.panels.filter((p) => p.draft);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load bloodwork';
		} finally {
			loading = false;
		}
	}

	function formatDate(iso: string): string {
		try {
			const d = new Date(iso + (iso.includes('T') ? '' : 'T00:00:00Z'));
			return d.toLocaleDateString(undefined, {
				year: 'numeric',
				month: '2-digit',
				day: '2-digit',
			});
		} catch {
			return iso;
		}
	}

	function formatRange(low: number | null, high: number | null): string {
		if (low == null && high == null) return '';
		if (low == null) return `≤ ${high}`;
		if (high == null) return `≥ ${low}`;
		return `${low}–${high}`;
	}

	function cell(panelId: number, markerName: string): { value: number; unit: string; flag: string | null } | null {
		const row = matrix?.values[String(panelId)];
		return row?.[markerName] ?? null;
	}

	function flagClass(flag: string | null): string {
		if (!flag) return '';
		return `flag-${flag}`;
	}

	function categoryLabel(c: string): string {
		const labels: Record<string, string> = {
			CBC: 'MORPHOLOGY',
			CMP: 'CHEMISTRY',
			Liver: 'LIVER',
			Lipid: 'LIPID PANEL',
			Thyroid: 'THYROID',
			Iron: 'IRON',
			Vitamins: 'VITAMINS',
			Inflammation: 'INFLAMMATION',
			Hormones: 'HORMONES',
			Diabetes: 'DIABETES',
			Other: 'OTHER',
		};
		return labels[c] || c.toUpperCase();
	}

	function encodeMarker(name: string): string {
		return encodeURIComponent(name);
	}

	// Use the canonical name (with underscores → spaces) as the column
	// header — that gives MCHC instead of "Mean Corpuscular Hemoglobin
	// Concentration", which is what we'd write on paper. The full
	// display_name is kept on the link's title attribute as a tooltip.
	// A small abbreviation map handles the cases the canonical name
	// can't shorten on its own.
	const ABBREVIATIONS: Record<string, string> = {
		'Vitamin D': 'Vit D',
		'Vitamin B12': 'Vit B12',
		'Cholesterol HDL Ratio': 'Chol/HDL',
		'Iron Saturation': 'Iron Sat',
	};

	function shortHeader(canonical: string): string {
		const spaced = canonical.replace(/_/g, ' ');
		return ABBREVIATIONS[spaced] || spaced;
	}

	onMount(load);
</script>

<div class="header">
	<h1>Bloodwork</h1>
	<div class="actions">
		<button class="btn" type="button" onclick={triggerCsvPick} disabled={csvImporting}>
			{csvImporting ? 'Importing…' : 'Import CSV'}
		</button>
		<input
			bind:this={csvInput}
			type="file"
			accept=".csv,text/csv"
			style="display: none"
			onchange={onCsvPicked}
		/>
		<a class="btn" href={healthCsvExportUrl()} download="bloodwork.csv">Export CSV</a>
		<a class="btn primary" href="{base}/health/bloodwork/upload">Upload lab results</a>
	</div>
</div>

{#if csvSummary}
	<div class="msg info">
		{#if csvSummary.panels_created > 0}
			Added {csvSummary.panels_created} panel{csvSummary.panels_created === 1 ? '' : 's'}
			({csvSummary.biomarkers_created} biomarkers).
		{:else}
			Nothing new to add.
		{/if}
		{#if csvSummary.panels_skipped_identical > 0}
			{csvSummary.panels_skipped_identical} already on file —
			{csvSummary.panels_skipped_identical === 1 ? 'it was' : 'they were'} skipped.
		{/if}
		{#if csvSummary.panels_needs_review > 0}
			{csvSummary.panels_needs_review} differ from existing panel{csvSummary.panels_needs_review === 1 ? '' : 's'}
			for the same date + lab — saved as drafts for review below.
		{/if}
		{#if csvSummary.warnings.length > 0}
			<details>
				<summary>{csvSummary.warnings.length} warning{csvSummary.warnings.length === 1 ? '' : 's'}</summary>
				<ul>{#each csvSummary.warnings as w}<li>{w}</li>{/each}</ul>
			</details>
		{/if}
	</div>
{/if}

{#if loading}
	<div class="loading">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else if matrix && matrix.panels.length === 0 && drafts.length === 0}
	<div class="empty">
		No bloodwork on file yet.
		<a href="{base}/health/bloodwork/upload">Upload your first lab report.</a>
	</div>
{:else if matrix}
	{#if drafts.length > 0}
		<section class="drafts">
			<h2>Pending review</h2>
			<ul>
				{#each drafts as p (p.id)}
					<li>
						<a href="{base}/health/bloodwork/panel?id={p.id}">
							<span class="badge">DRAFT</span>
							<span>{formatDate(p.drawn_at)}</span>
							<span class="muted">{p.lab_name || '—'}</span>
							<span class="muted">{p.panel_type || ''}</span>
						</a>
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if matrix.panels.length === 0}
		<div class="empty">
			No confirmed panels yet.
			{#if drafts.length > 0}
				Review the draft above to add it to your history,
			{/if}
			or
			<a href="{base}/health/bloodwork/upload">upload another lab report</a>.
		</div>
	{:else}
	<section class="spreadsheet">
		<div class="scroll">
			<table>
				<thead>
					<tr class="categories">
						<th class="sticky-left date-col"></th>
						<th class="sticky-left lab-col"></th>
						{#each matrix.categories as cat, ci (cat.name)}
							<th
								class="cat-cell"
								class:section-start={ci > 0}
								data-category={cat.name}
								colspan={cat.markers.length}
							>
								{categoryLabel(cat.name)}
							</th>
						{/each}
					</tr>
					<tr class="markers">
						<th class="sticky-left date-col">Date</th>
						<th class="sticky-left lab-col">Lab</th>
						{#each matrix.categories as cat, ci (cat.name)}
							{#each cat.markers as mk, mi (mk.name)}
								<th data-category={cat.name} class:section-start={ci > 0 && mi === 0}>
									<a
										href="{base}/health/bloodwork/marker?name={encodeMarker(mk.name)}"
										class="marker-link"
										title={mk.display_name}
									>
										<span class="marker-name">{shortHeader(mk.name)}</span>
										{#if mk.unit}<span class="marker-unit">{mk.unit}</span>{/if}
									</a>
								</th>
							{/each}
						{/each}
					</tr>
					<tr class="reference">
						<th class="sticky-left date-col">Reference range</th>
						<th class="sticky-left lab-col"></th>
						{#each matrix.categories as cat, ci (cat.name)}
							{#each cat.markers as mk, mi (mk.name)}
								<th class="ref" data-category={cat.name} class:section-start={ci > 0 && mi === 0}>{formatRange(mk.ref_range_low, mk.ref_range_high)}</th>
							{/each}
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each matrix.panels as p (p.id)}
						<tr>
							<td class="sticky-left date-col">
								<a href="{base}/health/bloodwork/panel?id={p.id}">{formatDate(p.drawn_at)}</a>
							</td>
							<td class="sticky-left lab-col">{p.lab_name || ''}</td>
							{#each matrix.categories as cat, ci (cat.name)}
								{#each cat.markers as mk, mi (mk.name)}
									{@const c = cell(p.id, mk.name)}
									<td class="{flagClass(c?.flag ?? null)}{ci > 0 && mi === 0 ? ' section-start' : ''}" data-category={cat.name}>
										{#if c}{c.value}{/if}
									</td>
								{/each}
							{/each}
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
	{/if}
{/if}

<style>
	.header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 1rem;
	}
	h1 {
		font-size: var(--text-lg);
		font-weight: 500;
		margin: 0;
	}
	.actions {
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}
	.btn {
		padding: 0.4rem 0.85rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		color: var(--text-primary);
		text-decoration: none;
		font: inherit;
		font-size: var(--text-sm);
		cursor: pointer;
	}
	.btn:disabled { opacity: 0.6; cursor: not-allowed; }
	.btn:hover:not(:disabled) { background: var(--surface-raised); }
	.btn.primary { border-color: #7aa3d8; color: #7aa3d8; }
	.msg {
		font-size: var(--text-sm);
		padding: 0.5rem 0.75rem;
		border-radius: 0.3rem;
		margin-bottom: 0.5rem;
	}
	.msg.info { background: rgba(122, 163, 216, 0.1); color: #7aa3d8; }
	.msg details { margin-top: 0.25rem; }
	.msg summary { cursor: pointer; }
	.msg ul { margin: 0.25rem 0 0 1rem; padding: 0; }

	.drafts {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.75rem 1rem;
		margin-bottom: 1rem;
	}
	.drafts h2 {
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
		margin: 0 0 0.5rem;
		font-weight: 500;
	}
	.drafts ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; }
	.drafts a {
		display: grid;
		grid-template-columns: auto 7rem 1fr 1fr;
		gap: 0.6rem;
		align-items: center;
		padding: 0;
		border-radius: 0.3rem;
		color: var(--text-primary);
		text-decoration: none;
		font-size: var(--text-sm);
	}
	.drafts a:hover { background: var(--surface-raised); }
	.badge {
		font-size: var(--text-xs);
		padding: 0 0.4rem;
		border-radius: var(--radius-pill);
		background: #3a3017;
		color: #e6b96b;
	}
	.muted { color: var(--text-muted); }
	.empty { color: var(--text-dim); padding: 2rem 0; }

	.spreadsheet {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		overflow: hidden;
	}
	.scroll {
		overflow: auto;
		max-height: calc(100vh - 200px);
	}
	table {
		border-collapse: separate;
		border-spacing: 0;
		font-size: var(--text-xs);
		min-width: 100%;
	}
	thead th {
		position: sticky;
		top: 0;
		background: var(--surface-card);
		z-index: 2;
		text-align: center;
		padding: 0.3rem 0.4rem;
		border-bottom: 1px solid var(--border-subtle);
		font-weight: 400;
		color: var(--text-muted);
		white-space: nowrap;
	}
	thead tr.categories th {
		top: 0;
		background: var(--surface-raised);
		font-size: var(--text-xs);
		font-weight: 500;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		text-transform: uppercase;
	}
	thead tr.markers th {
		top: 1.65rem;
		text-transform: none;
		letter-spacing: 0;
		color: var(--text-primary);
	}
	thead tr.reference th {
		top: 5rem;
		color: var(--text-dim);
		font-style: italic;
	}
	/* Section divider — runs the full height of each biomarker group on
	   its first column. Uses ``--border-subtle`` (the same value as the
	   thead bottom border) so the section break reads as the same kind
	   of structural line. First group at the table's left edge has no
	   divider — it's already bounded by the sticky Lab column's right
	   border. */
	.cat-cell.section-start,
	thead tr.markers th.section-start,
	thead tr.reference th.section-start,
	tbody td.section-start {
		border-left: 1px solid var(--border-subtle);
	}
	thead tr.markers th {
		vertical-align: bottom;
		height: 3.4rem;
	}
	.marker-link {
		display: inline-flex;
		flex-direction: column;
		align-items: center;
		gap: 0.05rem;
		color: inherit;
		text-decoration: none;
		width: 100%;
		min-width: 4.5rem;
		max-width: 7rem;
		padding: 0 0.3rem;
	}
	.marker-link:hover .marker-name {
		text-decoration: underline;
	}
	.marker-name {
		font-size: var(--text-xs);
		font-weight: 500;
		text-align: center;
		line-height: 1.15;
		/* Up to two lines; anything longer ellipsises and the full name
		   is on the link's title attribute as a tooltip. */
		white-space: normal;
		display: -webkit-box;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		overflow: hidden;
		word-break: break-word;
	}
	.marker-unit {
		font-size: 10px;
		color: var(--text-dim);
		white-space: nowrap;
	}
	.ref {
		font-size: 10px;
	}
	tbody td {
		text-align: center;
		padding: 0.25rem 0.4rem;
		border-bottom: 1px solid var(--border-subtle);
		white-space: nowrap;
		color: var(--text-primary);
	}

	/* Per-category palette. Each section (Thyroid, Lipid, …) carries its
	   own hue end-to-end: the category banner + marker-name row share the
	   stronger tint so they read as one header block, and the reference
	   row + data cells get a subtler version of the same hue. Hues are
	   stored as CSS vars so the two intensity levels can share one rule
	   pair. */
	[data-category="CBC"]          { --cat-h: 0;   --cat-s: 50%; }
	[data-category="CMP"]          { --cat-h: 210; --cat-s: 45%; }
	[data-category="Liver"]        { --cat-h: 30;  --cat-s: 55%; }
	[data-category="Lipid"]        { --cat-h: 145; --cat-s: 40%; }
	[data-category="Thyroid"]      { --cat-h: 280; --cat-s: 45%; }
	[data-category="Iron"]         { --cat-h: 18;  --cat-s: 60%; }
	[data-category="Vitamins"]     { --cat-h: 50;  --cat-s: 55%; }
	[data-category="Inflammation"] { --cat-h: 335; --cat-s: 50%; }
	[data-category="Hormones"]     { --cat-h: 185; --cat-s: 45%; }
	[data-category="Diabetes"]     { --cat-h: 85;  --cat-s: 45%; }
	[data-category="Other"]        { --cat-h: 220; --cat-s: 8%;  }

	thead tr.categories th[data-category],
	thead tr.markers th[data-category] {
		background: hsla(var(--cat-h), var(--cat-s), 65%, 0.22);
	}
	thead tr.reference th[data-category],
	tbody td[data-category] {
		background: hsla(var(--cat-h), var(--cat-s), 65%, 0.08);
	}

	/* Sticky left columns sit above every row's data, fully opaque so
	   scrolling values never bleed through. */
	td.sticky-left,
	th.sticky-left {
		position: sticky;
		left: 0;
		background: var(--surface-card);
		z-index: 3;
		text-align: left;
		border-right: 1px solid var(--border-default);
	}
	thead th.sticky-left {
		z-index: 4;
	}
	.date-col {
		min-width: 6.5rem;
	}
	.lab-col {
		left: 6.5rem;
		min-width: 9rem;
		font-size: var(--text-xs);
		color: var(--text-muted);
	}
	tbody .sticky-left a {
		color: var(--text-primary);
		text-decoration: none;
	}
	tbody .sticky-left a:hover {
		text-decoration: underline;
	}
	tbody td.flag-H { background: rgba(204, 102, 102, 0.22); color: #f8a09c; }
	tbody td.flag-L { background: rgba(122, 163, 216, 0.22); color: #9cc7f8; }
	tbody td.flag-C { background: #6b0000; color: #fff; font-weight: 500; }
	.msg.error {
		font-size: var(--text-sm);
		padding: 0.5rem;
		border-radius: 0.3rem;
		background: rgba(204, 102, 102, 0.1);
		color: #f0a;
	}

	@media (max-width: 768px) {
		.header {
			flex-direction: column;
			align-items: stretch;
			gap: 0.5rem;
			margin-bottom: 0.75rem;
		}
		.actions {
			gap: 0.3rem;
			flex-wrap: nowrap;
		}
		.btn {
			padding: 0.2rem 0.5rem;
			font-size: var(--text-xs);
			white-space: nowrap;
			flex: 0 0 auto;
		}
		.drafts {
			padding: 0.75rem 0.6rem;
		}
		.drafts a {
			/* Stack the columns; the desktop 4-column grid wraps off-screen. */
			grid-template-columns: auto auto 1fr;
			grid-template-rows: auto auto;
			gap: 0.2rem 0.5rem;
			font-size: var(--text-xs);
		}
		.drafts a > :nth-child(4) {
			grid-column: 1 / -1;
			font-size: 11px;
		}
		.spreadsheet {
			border-radius: 0;
			border-left: none;
			border-right: none;
			margin: 0 -0.75rem;
		}
		.scroll {
			max-height: calc(100vh - 160px);
		}
		/* Date stays fixed on horizontal scroll; lab scrolls away
		   because it's the longer column and would eat the viewport. */
		td.lab-col,
		th.lab-col {
			position: static;
			left: auto;
			min-width: 7rem;
		}
	}
</style>
