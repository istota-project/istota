<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getImmunizationCoverage,
		listImmunizations,
		listImmunizationRefs,
		createImmunization,
		deleteImmunization,
		type CoverageEntry,
		type Immunization,
		type ImmunizationRef,
		type ImmunizationStatus,
	} from '$lib/api';

	let loading = $state(true);
	let error = $state('');
	let coverage: CoverageEntry[] = $state([]);
	let other: CoverageEntry[] = $state([]);
	let history: Immunization[] = $state([]);
	let refs: ImmunizationRef[] = $state([]);
	let nameFilter = $state('');

	// Quick-log form
	let formOpen = $state(false);
	let formName = $state('Influenza');
	let formDate = $state(new Date().toISOString().slice(0, 10));
	let formProduct = $state('');
	let formFacility = $state('');
	let formLot = $state('');
	let formRoute = $state('');
	let formSite = $state('');
	let formNotes = $state('');
	let saving = $state(false);
	let formError = $state('');

	async function load() {
		loading = true;
		error = '';
		try {
			const [cov, hist, refResp] = await Promise.all([
				getImmunizationCoverage(),
				listImmunizations({
					name: nameFilter || undefined,
					limit: 500,
				}),
				listImmunizationRefs(),
			]);
			coverage = cov.coverage;
			other = cov.other;
			history = hist.immunizations;
			refs = refResp.refs;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load immunizations';
		} finally {
			loading = false;
		}
	}

	async function submit(e: Event) {
		e.preventDefault();
		formError = '';
		saving = true;
		try {
			await createImmunization({
				name: formName,
				date_given: formDate,
				product_name: formProduct || undefined,
				facility: formFacility || undefined,
				lot_number: formLot || undefined,
				route: formRoute || undefined,
				site: formSite || undefined,
				notes: formNotes || undefined,
			});
			formProduct = '';
			formFacility = '';
			formLot = '';
			formRoute = '';
			formSite = '';
			formNotes = '';
			formOpen = false;
			await load();
		} catch (e) {
			formError = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}

	async function deleteRow(id: number) {
		if (!confirm('Delete this immunization?')) return;
		try {
			await deleteImmunization(id);
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete';
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

	// Sort: action-needed first (overdue, due_soon, expired), then
	// series_incomplete, then up_to_date, then never_recorded, then
	// risk_based (collapsed below).
	const statusOrder: Record<ImmunizationStatus, number> = {
		overdue: 0,
		expired: 0,
		due_soon: 1,
		series_incomplete: 2,
		up_to_date: 3,
		never_recorded: 4,
		risk_based: 5,
		recorded: 6,
	};

	const visibleCoverage = $derived(
		coverage
			.filter((c) => c.category !== 'risk_based')
			.slice()
			.sort(
				(a, b) =>
					(statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9) ||
					a.display_name.localeCompare(b.display_name),
			),
	);

	const riskBased = $derived(
		coverage.filter((c) => c.category === 'risk_based'),
	);

	let riskOpen = $state(false);

	onMount(load);
</script>

<div class="header">
	<h1>Immunizations</h1>
	<div class="actions">
		<button class="btn" type="button" onclick={() => (formOpen = !formOpen)}>
			{formOpen ? 'Cancel' : '+ Log dose'}
		</button>
		<a class="btn" href="{base}/health/immunizations/paste">Import from paste</a>
	</div>
</div>

{#if formOpen}
	<form class="quick-form" onsubmit={submit}>
		<div class="row">
			<label>
				<span>Vaccine</span>
				<select bind:value={formName}>
					{#each refs as r (r.name)}
						<option value={r.name}>{r.display_name}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>Date</span>
				<input type="date" bind:value={formDate} required />
			</label>
			<label>
				<span>Product</span>
				<input type="text" bind:value={formProduct} placeholder="Fluzone Quadrivalent" />
			</label>
			<label>
				<span>Facility</span>
				<input type="text" bind:value={formFacility} placeholder="CVS Pharmacy" />
			</label>
		</div>
		<details class="advanced">
			<summary>More fields</summary>
			<div class="row">
				<label>
					<span>Lot number</span>
					<input type="text" bind:value={formLot} />
				</label>
				<label>
					<span>Route</span>
					<select bind:value={formRoute}>
						<option value=""></option>
						<option value="IM">IM</option>
						<option value="SC">SC</option>
						<option value="oral">Oral</option>
						<option value="nasal">Nasal</option>
					</select>
				</label>
				<label>
					<span>Site</span>
					<input type="text" bind:value={formSite} placeholder="left deltoid" />
				</label>
			</div>
			<label class="full">
				<span>Notes</span>
				<textarea bind:value={formNotes} rows="2"></textarea>
			</label>
		</details>
		{#if formError}
			<div class="msg error">{formError}</div>
		{/if}
		<div class="form-actions">
			<button type="submit" class="btn primary" disabled={saving}>
				{saving ? 'Saving…' : 'Save'}
			</button>
		</div>
	</form>
{/if}

{#if loading}
	<div class="empty">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else}
	<section class="coverage">
		<h2>Coverage</h2>
		<div class="cards">
			{#each visibleCoverage as c (c.name)}
				<a class="card status-{c.status}" href="{base}/health/immunizations/vaccine?name={encodeURIComponent(c.name)}">
					<div class="card-head">
						<span class="name">{c.display_name}</span>
						<span class="badge status-{c.status}">{statusLabel(c.status)}</span>
					</div>
					<div class="card-body">
						<div class="muted">
							{#if c.last_given}
								Last: {formatDate(c.last_given)}
								{#if c.dose_count > 1} · {c.dose_count} doses{/if}
							{:else}
								No record
							{/if}
						</div>
						{#if c.next_due}
							<div class="muted">
								Next due: {formatDate(c.next_due)}
								{#if c.days_until_due !== null}
									{#if c.days_until_due < 0}
										({-c.days_until_due}d overdue)
									{:else}
										(in {c.days_until_due}d)
									{/if}
								{/if}
							</div>
						{/if}
					</div>
				</a>
			{/each}
		</div>

		{#if riskBased.length > 0}
			<details class="risk-based" bind:open={riskOpen}>
				<summary>Risk-based vaccines ({riskBased.length})</summary>
				<div class="cards">
					{#each riskBased as c (c.name)}
						<a class="card status-{c.status}" href="{base}/health/immunizations/vaccine?name={encodeURIComponent(c.name)}">
							<div class="card-head">
								<span class="name">{c.display_name}</span>
								<span class="badge status-{c.status}">{statusLabel(c.status)}</span>
							</div>
							<div class="card-body">
								<div class="muted">
									{#if c.last_given}
										Last: {formatDate(c.last_given)}
									{:else}
										Not recorded
									{/if}
								</div>
							</div>
						</a>
					{/each}
				</div>
			</details>
		{/if}

		{#if other.length > 0}
			<h3>Other recorded</h3>
			<div class="cards">
				{#each other as c (c.name)}
					<div class="card status-recorded">
						<div class="card-head">
							<span class="name">{c.display_name}</span>
							<span class="badge status-recorded">{c.dose_count} dose{c.dose_count > 1 ? 's' : ''}</span>
						</div>
						<div class="card-body">
							<div class="muted">Last: {formatDate(c.last_given)}</div>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</section>

	<section class="history">
		<div class="history-head">
			<h2>History</h2>
			<input
				type="text"
				placeholder="Filter by vaccine name"
				bind:value={nameFilter}
				onchange={load}
			/>
		</div>
		{#if history.length === 0}
			<div class="empty small">No immunizations recorded yet.</div>
		{:else}
			<table>
				<thead>
					<tr>
						<th>Date</th>
						<th>Vaccine</th>
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
							<td>
								<a href="{base}/health/immunizations/vaccine?name={encodeURIComponent(i.name)}">
									{i.name}
								</a>
							</td>
							<td>{i.product_name || '—'}</td>
							<td>{i.dose_label || '—'}</td>
							<td>{i.facility || '—'}</td>
							<td class="notes">{i.notes || '—'}</td>
							<td class="actions">
								<a class="btn small" href="{base}/health/immunizations/detail?id={i.id}">Edit</a>
								<button class="btn small danger" type="button" onclick={() => deleteRow(i.id)}>Delete</button>
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
		gap: 1rem;
		margin-bottom: 1rem;
	}
	.header h1 {
		font-size: 1.5rem;
		margin: 0;
	}
	.actions {
		display: flex;
		gap: 0.5rem;
	}
	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.4rem 0.75rem;
		border: 1px solid var(--border, #ddd);
		border-radius: 6px;
		background: var(--surface, #fff);
		color: var(--text, #222);
		text-decoration: none;
		font-size: 0.875rem;
		cursor: pointer;
	}
	.btn.primary {
		background: var(--accent, #2a6df4);
		color: #fff;
		border-color: var(--accent, #2a6df4);
	}
	.btn.small {
		padding: 0.25rem 0.5rem;
		font-size: 0.8rem;
	}
	.btn.danger {
		color: var(--danger, #c0392b);
	}
	.quick-form {
		border: 1px solid var(--border, #ddd);
		border-radius: 8px;
		padding: 1rem;
		margin-bottom: 1rem;
		background: var(--surface, #fff);
	}
	.row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 0.75rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.85rem;
	}
	label.full {
		display: block;
		margin-top: 0.75rem;
	}
	label span {
		color: var(--muted, #666);
		font-size: 0.75rem;
	}
	input,
	select,
	textarea {
		padding: 0.4rem 0.5rem;
		border: 1px solid var(--border, #ddd);
		border-radius: 4px;
		background: var(--surface, #fff);
		color: var(--text, #222);
		font: inherit;
	}
	textarea {
		resize: vertical;
		min-height: 3em;
	}
	.advanced {
		margin-top: 0.75rem;
	}
	.advanced summary {
		cursor: pointer;
		color: var(--muted, #666);
		font-size: 0.85rem;
	}
	.form-actions {
		margin-top: 0.75rem;
		display: flex;
		justify-content: flex-end;
	}
	.msg.error {
		color: var(--danger, #c0392b);
		font-size: 0.85rem;
		margin: 0.5rem 0;
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
	.coverage {
		margin-bottom: 1.5rem;
	}
	.coverage h2,
	.history h2 {
		font-size: 1.1rem;
		margin: 0 0 0.75rem;
	}
	.coverage h3 {
		font-size: 0.95rem;
		margin: 1rem 0 0.5rem;
		color: var(--muted, #666);
	}
	.cards {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: 0.5rem;
	}
	.card {
		border: 1px solid var(--border, #ddd);
		border-left: 3px solid var(--border, #ddd);
		border-radius: 6px;
		padding: 0.75rem;
		background: var(--surface, #fff);
		color: inherit;
		text-decoration: none;
		display: block;
	}
	.card-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
		margin-bottom: 0.5rem;
	}
	.name {
		font-weight: 600;
		font-size: 0.95rem;
	}
	.muted {
		color: var(--muted, #666);
		font-size: 0.8rem;
	}
	.badge {
		display: inline-block;
		padding: 0.1rem 0.4rem;
		border-radius: 4px;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.02em;
		background: #eee;
		color: #555;
		white-space: nowrap;
	}
	.card.status-overdue,
	.card.status-expired {
		border-left-color: #d44;
	}
	.badge.status-overdue,
	.badge.status-expired {
		background: #fde6e6;
		color: #a22;
	}
	.card.status-due_soon {
		border-left-color: #f0a020;
	}
	.badge.status-due_soon {
		background: #fff1d6;
		color: #8a5a00;
	}
	.card.status-series_incomplete {
		border-left-color: #d0a;
	}
	.badge.status-series_incomplete {
		background: #fbe6f4;
		color: #8a0668;
	}
	.card.status-up_to_date {
		border-left-color: #2a8;
	}
	.badge.status-up_to_date {
		background: #dff5e8;
		color: #186b3a;
	}
	.card.status-never_recorded,
	.card.status-recorded {
		border-left-color: #aaa;
	}
	.badge.status-never_recorded,
	.badge.status-recorded {
		background: #eee;
		color: #555;
	}
	.card.status-risk_based {
		border-left-color: #888;
	}
	.badge.status-risk_based {
		background: #eee;
		color: #555;
	}
	.risk-based {
		margin-top: 1rem;
	}
	.risk-based summary {
		cursor: pointer;
		font-size: 0.9rem;
		padding: 0.25rem 0;
		color: var(--muted, #666);
	}
	.risk-based[open] summary {
		margin-bottom: 0.5rem;
	}
	.history-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 1rem;
		margin-bottom: 0.5rem;
	}
	.history-head input {
		max-width: 240px;
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
	td.actions {
		display: flex;
		gap: 0.25rem;
		justify-content: flex-end;
	}
	td.notes {
		max-width: 300px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
