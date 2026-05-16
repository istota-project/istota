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

		// Fetch the explainer separately — it's allowed to fail without
		// blocking the rest of the page.
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
	<div class="empty">Unknown vaccine "{name}". It may not be in the canonical reference list.</div>
{:else}
	<section class="coverage-card">
		{#if entry}
			<div class="status">
				<span class="badge status-{entry.status}">{statusLabel(entry.status)}</span>
			</div>
			<div class="grid">
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
				<div>
					<dt>Category / schedule</dt>
					<dd>{ref.category} · {ref.schedule}</dd>
				</div>
			</div>
		{/if}
		{#if ref.description}
			<p class="description">{ref.description}</p>
		{/if}
		{#if ref.typical_age_range}
			<p class="muted small">Typical age range: {ref.typical_age_range}</p>
		{/if}
	</section>

	{#if explainerLoading}
		<section class="explainer placeholder">
			<h2>About this vaccine</h2>
			<p class="muted">Loading…</p>
		</section>
	{:else if explainer && explainer.source !== 'skipped'}
		<section class="explainer">
			<h2>About this vaccine</h2>
			<p>{explainer.summary}</p>
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
				<p class="muted small disclaimer">{explainer.disclaimer}</p>
			{/if}
		</section>
	{/if}

	<section class="history">
		<h2>Dose history</h2>
		{#if history.length === 0}
			<div class="empty small">No doses recorded yet.</div>
		{:else}
			<table>
				<thead>
					<tr>
						<th>Date</th>
						<th>Product</th>
						<th>Dose label</th>
						<th>Facility</th>
						<th>Notes</th>
						<th></th>
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
							<td>
								<a class="btn small" href="{base}/health/immunizations/detail?id={i.id}">
									Edit
								</a>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</section>
{/if}

<style>
	.header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 1rem;
	}
	.header h1 {
		font-size: 1.5rem;
		margin: 0;
	}
	.btn {
		display: inline-flex;
		padding: 0.4rem 0.75rem;
		border: 1px solid var(--border, #ddd);
		border-radius: 6px;
		background: var(--surface, #fff);
		color: inherit;
		text-decoration: none;
		font-size: 0.875rem;
	}
	.btn.small {
		padding: 0.25rem 0.5rem;
		font-size: 0.8rem;
	}
	.coverage-card {
		border: 1px solid var(--border, #ddd);
		border-radius: 8px;
		padding: 1rem;
		margin-bottom: 1.25rem;
		background: var(--surface, #fff);
	}
	.status {
		margin-bottom: 0.5rem;
	}
	.badge {
		display: inline-block;
		padding: 0.2rem 0.5rem;
		border-radius: 4px;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.02em;
	}
	.badge.status-overdue,
	.badge.status-expired {
		background: #fde6e6;
		color: #a22;
	}
	.badge.status-due_soon {
		background: #fff1d6;
		color: #8a5a00;
	}
	.badge.status-series_incomplete {
		background: #fbe6f4;
		color: #8a0668;
	}
	.badge.status-up_to_date {
		background: #dff5e8;
		color: #186b3a;
	}
	.badge.status-never_recorded,
	.badge.status-recorded,
	.badge.status-risk_based {
		background: #eee;
		color: #555;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.75rem;
		margin: 0.75rem 0;
	}
	dt {
		color: var(--muted, #666);
		font-size: 0.75rem;
		margin-bottom: 0.15rem;
	}
	dd {
		margin: 0;
		font-size: 0.95rem;
	}
	.description {
		margin: 0.75rem 0 0;
	}
	.muted {
		color: var(--muted, #666);
	}
	.small {
		font-size: 0.85rem;
	}
	.explainer {
		border: 1px solid var(--border, #ddd);
		border-left: 3px solid var(--accent, #2a6df4);
		border-radius: 8px;
		padding: 1rem;
		margin-bottom: 1.25rem;
		background: var(--surface, #fff);
	}
	.explainer.placeholder {
		border-left-color: var(--border, #ddd);
	}
	.explainer h2 {
		font-size: 1.05rem;
		margin: 0 0 0.5rem;
	}
	.explainer h3 {
		font-size: 0.9rem;
		margin: 0.75rem 0 0.25rem;
		color: var(--muted, #666);
	}
	.explainer ul {
		margin: 0;
		padding-left: 1.25rem;
	}
	.explainer li {
		margin: 0.25rem 0;
	}
	.disclaimer {
		margin-top: 0.75rem;
		font-style: italic;
	}
	.history h2 {
		font-size: 1.05rem;
		margin: 0 0 0.5rem;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.875rem;
	}
	th,
	td {
		text-align: left;
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--border, #eee);
	}
	td.notes {
		max-width: 280px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.empty {
		padding: 2rem;
		text-align: center;
		color: var(--muted, #666);
	}
	.empty.small {
		padding: 1rem;
		font-size: 0.875rem;
	}
	.msg.error {
		color: var(--danger, #c0392b);
		font-size: 0.85rem;
		margin: 0.5rem 0;
	}
</style>
