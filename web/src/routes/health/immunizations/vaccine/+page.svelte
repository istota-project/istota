<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import {
		getImmunizationCoverage,
		getImmunizationExplainer,
		listImmunizations,
		listImmunizationRefs,
		type CoverageEntry,
		type Immunization,
		type ImmunizationExplainer,
		type ImmunizationRef,
		type ImmunizationStatus,
	} from '$lib/api';

	let name = $derived(page.url.searchParams.get('name') || '');
	let loading = $state(true);
	let error = $state('');
	let ref: ImmunizationRef | null = $state(null);
	let entry: CoverageEntry | null = $state(null);
	let history: Immunization[] = $state([]);
	let explainer: ImmunizationExplainer | null = $state(null);
	let explainerLoading = $state(false);

	async function load() {
		if (!name) return;
		loading = true;
		error = '';
		try {
			const [refResp, cov, hist] = await Promise.all([
				listImmunizationRefs(),
				getImmunizationCoverage(),
				listImmunizations({ name, limit: 200 }),
			]);
			ref = refResp.refs.find((r) => r.name === name) ?? null;
			entry = cov.coverage.find((c) => c.name === name) ?? null;
			history = hist.immunizations;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load';
		} finally {
			loading = false;
		}

		if (entry) {
			explainerLoading = true;
			try {
				explainer = await getImmunizationExplainer(name);
			} catch {
				explainer = null;
			} finally {
				explainerLoading = false;
			}
		}
	}

	function formatDate(iso: string | null): string {
		if (!iso) return '—';
		try {
			const d = new Date(iso + (iso.includes('T') ? '' : 'T00:00:00Z'));
			return d.toLocaleDateString(undefined, {
				year: 'numeric',
				month: 'short',
				day: 'numeric',
			});
		} catch {
			return iso;
		}
	}

	function statusLabel(s: ImmunizationStatus): string {
		const m: Record<ImmunizationStatus, string> = {
			up_to_date: 'Up to date',
			due_soon: 'Due soon',
			overdue: 'Overdue',
			series_incomplete: 'Series incomplete',
			never_recorded: 'Never recorded',
			expired: 'Expired',
			risk_based: 'Risk-based',
			recorded: 'Recorded',
		};
		return m[s] ?? s;
	}

	$effect(() => {
		if (name) load();
	});

	onMount(() => {
		if (name) load();
	});
</script>

<div class="header">
	<h1>{ref?.display_name || name}</h1>
	<a class="btn" href="{base}/health/immunizations">Back</a>
</div>

{#if loading}
	<div class="empty">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else if !ref}
	<div class="empty">
		Unknown vaccine "{name}". It may not be in the canonical reference list.
	</div>
{:else}
	<section class="card coverage-card">
		{#if entry}
			<div class="status-row">
				<span class="badge status-{entry.status}">{statusLabel(entry.status)}</span>
				<span class="muted small">{ref.category} · {ref.schedule}</span>
			</div>
			<dl class="grid-stats">
				<div>
					<dt>Last given</dt>
					<dd>{formatDate(entry.last_given)}</dd>
				</div>
				<div>
					<dt>Doses recorded</dt>
					<dd>{entry.dose_count}</dd>
				</div>
				<div>
					<dt>Next due</dt>
					<dd>{formatDate(entry.next_due)}</dd>
				</div>
				{#if entry.days_until_due !== null}
					<div>
						<dt>{entry.days_until_due < 0 ? 'Days overdue' : 'Days until due'}</dt>
						<dd>{Math.abs(entry.days_until_due)}</dd>
					</div>
				{/if}
			</dl>
		{/if}
		{#if ref.description}
			<p class="description">{ref.description}</p>
		{/if}
		{#if ref.typical_age_range}
			<p class="muted small">Typical age range: {ref.typical_age_range}</p>
		{/if}
	</section>

	{#if explainerLoading}
		<section class="card explainer placeholder">
			<h2>About this vaccine</h2>
			<p class="muted">Loading…</p>
		</section>
	{:else if explainer && explainer.source !== 'skipped'}
		<section class="card explainer">
			<h2>About this vaccine</h2>
			<p class="summary">{explainer.summary}</p>
			{#if explainer.why_it_matters.length > 0}
				<h3>Why it matters</h3>
				<ul>
					{#each explainer.why_it_matters as item (item)}
						<li>{item}</li>
					{/each}
				</ul>
			{/if}
			{#if explainer.considerations.length > 0}
				<h3>Things to consider</h3>
				<ul>
					{#each explainer.considerations as item (item)}
						<li>{item}</li>
					{/each}
				</ul>
			{/if}
			{#if explainer.disclaimer}
				<p class="disclaimer">{explainer.disclaimer}</p>
			{/if}
		</section>
	{/if}

	<section class="history">
		<h2>Dose history</h2>
		{#if history.length === 0}
			<div class="empty small">No doses recorded yet.</div>
		{:else}
			<div class="table-scroll">
				<table class="grid">
					<thead>
						<tr>
							<th>Date</th>
							<th>Product</th>
							<th>Dose label</th>
							<th>Facility</th>
							<th>Notes</th>
							<th class="row-actions"></th>
						</tr>
					</thead>
					<tbody>
						{#each history as i (i.id)}
							<tr>
								<td>{formatDate(i.date_given)}</td>
								<td>{i.product_name || '—'}</td>
								<td>{i.dose_label || '—'}</td>
								<td>{i.facility || '—'}</td>
								<td class="notes">{i.notes || '—'}</td>
								<td class="row-actions">
									<a class="btn small" href="{base}/health/immunizations/detail?id={i.id}">
										Edit
									</a>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</section>
{/if}

<style>
	.header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 1rem;
	}
	h1 {
		font-size: var(--text-lg, 1.05rem);
		font-weight: 500;
		margin: 0;
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
		line-height: 1.2;
	}
	.btn:hover:not(:disabled) { background: var(--surface-raised); }
	.btn.small {
		padding: 0.2rem 0.55rem;
		font-size: var(--text-xs);
	}

	.card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.85rem 1rem;
		margin-bottom: 1rem;
	}
	.coverage-card .status-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.5rem;
	}
	.grid-stats {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 0.75rem 1rem;
		margin: 0;
	}
	dt {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		margin-bottom: 0.15rem;
	}
	dd {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--text-primary);
	}
	.description {
		margin: 0.75rem 0 0;
		font-size: var(--text-sm);
		color: var(--text-secondary);
		line-height: 1.55;
		max-width: 75ch;
	}
	.muted {
		color: var(--text-muted);
	}
	.small {
		font-size: var(--text-xs);
	}

	.explainer h2 {
		margin: 0 0 0.5rem;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
		font-weight: 500;
	}
	.explainer h3 {
		margin: 0.85rem 0 0.35rem;
		font-size: var(--text-xs);
		font-weight: 500;
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.explainer .summary {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--text-secondary);
		line-height: 1.55;
		max-width: 75ch;
	}
	.explainer ul {
		margin: 0;
		padding-left: 1.1rem;
		font-size: var(--text-sm);
		color: var(--text-secondary);
		line-height: 1.55;
	}
	.explainer li {
		margin: 0.2rem 0;
	}
	.explainer .disclaimer {
		margin: 0.85rem 0 0;
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-style: italic;
	}
	.explainer.placeholder {
		opacity: 0.7;
	}

	.history h2 {
		margin: 0 0 0.5rem;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
		font-weight: 500;
	}
	.table-scroll {
		width: 100%;
		overflow-x: auto;
	}
	table.grid {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--text-sm);
	}
	table.grid th,
	table.grid td {
		text-align: left;
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--border-subtle);
		vertical-align: middle;
	}
	table.grid th {
		color: var(--text-dim);
		font-weight: 500;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	td.notes {
		max-width: 260px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--text-muted);
	}
	td.row-actions,
	th.row-actions {
		text-align: right;
		white-space: nowrap;
	}

	.badge {
		display: inline-flex;
		align-items: center;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0.1rem 0.5rem;
		border-radius: var(--radius-pill);
		font-weight: 500;
	}
	.badge.status-overdue,
	.badge.status-expired {
		background: hsla(0, 60%, 55%, 0.28);
		color: #ff9d96;
	}
	.badge.status-due_soon {
		background: hsla(35, 60%, 60%, 0.22);
		color: #e6b96b;
	}
	.badge.status-series_incomplete {
		background: hsla(280, 45%, 65%, 0.22);
		color: #d0aeec;
	}
	.badge.status-up_to_date {
		background: hsla(145, 40%, 55%, 0.22);
		color: #9bd6a6;
	}
	.badge.status-never_recorded,
	.badge.status-recorded,
	.badge.status-risk_based {
		background: hsla(220, 8%, 60%, 0.18);
		color: var(--text-muted);
	}

	.empty {
		color: var(--text-dim);
		padding: 2rem 0;
	}
	.empty.small {
		padding: 0.75rem 0;
		font-size: var(--text-sm);
	}
	.msg {
		font-size: var(--text-sm);
		padding: 0.4rem 0.6rem;
		border-radius: 0.3rem;
	}
	.msg.error {
		background: rgba(204, 102, 102, 0.1);
		color: #e88;
	}
</style>
