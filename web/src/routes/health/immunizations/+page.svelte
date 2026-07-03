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
				listImmunizations({ name: nameFilter || undefined, limit: 500 }),
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
			const d = new Date(iso + (iso.includes('T') ? '' : 'T00:00:00'));
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
		<a class="btn" href="{base}/health/immunizations/import">Import</a>
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
	<div class="loading">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else}
	<section class="coverage">
		<h2>Coverage</h2>
		<ul class="cards">
			{#each visibleCoverage as c (c.name)}
				<li>
					<a
						class="card"
						href="{base}/health/immunizations/vaccine?name={encodeURIComponent(c.name)}"
					>
						<span class="name">{c.display_name}</span>
						<span class="badge status-{c.status}">{statusLabel(c.status)}</span>
						<div class="card-body">
							<div class="muted">
								{#if c.last_given}
									Last: {formatDate(c.last_given)}{#if c.dose_count > 1} · {c.dose_count} doses{/if}
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
				</li>
			{/each}
		</ul>

		{#if riskBased.length > 0}
			<details class="risk-based" bind:open={riskOpen}>
				<summary>Risk-based vaccines ({riskBased.length})</summary>
				<ul class="cards">
					{#each riskBased as c (c.name)}
						<li>
							<a
								class="card"
								href="{base}/health/immunizations/vaccine?name={encodeURIComponent(c.name)}"
							>
								<span class="name">{c.display_name}</span>
								<span class="badge status-{c.status}">{statusLabel(c.status)}</span>
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
						</li>
					{/each}
				</ul>
			</details>
		{/if}

		{#if other.length > 0}
			<h3>Other recorded</h3>
			<ul class="cards">
				{#each other as c (c.name)}
					<li>
						<div class="card">
							<span class="name">{c.display_name}</span>
							<span class="badge status-recorded">
								{c.dose_count} dose{c.dose_count > 1 ? 's' : ''}
							</span>
							<div class="card-body">
								<div class="muted">Last: {formatDate(c.last_given)}</div>
							</div>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<section class="history">
		<div class="history-head">
			<h2>History</h2>
			<input
				type="text"
				class="filter-input"
				placeholder="Filter by vaccine name"
				bind:value={nameFilter}
				onchange={load}
			/>
		</div>
		{#if history.length === 0}
			<div class="empty small">No immunizations recorded yet.</div>
		{:else}
			<div class="table-scroll">
				<table class="grid">
					<thead>
						<tr>
							<th>Date</th>
							<th>Vaccine</th>
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
								<td>
									<a
										class="link"
										href="{base}/health/immunizations/vaccine?name={encodeURIComponent(i.name)}"
									>
										{i.name}
									</a>
								</td>
								<td>{i.product_name || '—'}</td>
								<td>{i.dose_label || '—'}</td>
								<td>{i.facility || '—'}</td>
								<td class="notes">{i.notes || '—'}</td>
								<td class="row-actions">
									<a class="btn small" href="{base}/health/immunizations/detail?id={i.id}">
										Edit
									</a>
									<button
										class="btn small danger"
										type="button"
										onclick={() => deleteRow(i.id)}
									>
										Delete
									</button>
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
		line-height: 1.2;
	}
	.btn:disabled { opacity: 0.6; cursor: not-allowed; }
	.btn:hover:not(:disabled) { background: var(--surface-raised); }
	.btn.primary { border-color: #7aa3d8; color: #7aa3d8; }
	.btn.small {
		padding: 0.2rem 0.55rem;
		font-size: var(--text-xs);
	}
	.btn.danger { color: var(--text-muted); }
	.btn.danger:hover:not(:disabled) { color: #e88; }

	.quick-form {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.85rem 1rem;
		margin-bottom: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.65rem;
	}
	.quick-form .row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.65rem;
	}
	.quick-form label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--text-sm);
		min-width: 0;
	}
	.quick-form label.full {
		grid-column: 1 / -1;
		margin-top: 0.25rem;
	}
	.quick-form label > span {
		color: var(--text-muted);
		font-size: var(--text-xs);
	}
	.quick-form input,
	.quick-form select,
	.quick-form textarea {
		padding: 0.3rem 0.5rem;
		background: var(--surface-raised);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		box-sizing: border-box;
		min-width: 0;
	}
	.quick-form textarea {
		resize: vertical;
		font-family: inherit;
	}
	.quick-form details.advanced > summary {
		color: var(--text-muted);
		font-size: var(--text-sm);
		cursor: pointer;
		user-select: none;
	}
	.form-actions {
		display: flex;
		justify-content: flex-end;
	}

	.coverage {
		margin-bottom: 1.25rem;
	}
	.coverage h2,
	.history h2 {
		margin: 0 0 0.5rem;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
		font-weight: 500;
	}
	.coverage h3 {
		margin: 0.85rem 0 0.4rem;
		font-size: var(--text-xs);
		font-weight: 500;
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.cards {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: 0.5rem;
		grid-auto-rows: 1fr;
	}
	.cards > li {
		display: flex;
	}
	.card {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.4rem;
		width: 100%;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.7rem 0.9rem;
		text-decoration: none;
		color: var(--text-primary);
	}
	.card:hover { border-color: #555; }
	.card .name {
		font-weight: 500;
		font-size: var(--text-sm);
		line-height: 1.35;
	}
	.card-body {
		margin-top: auto;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.muted {
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	/* Badges — match history page palette: HSLA on the dark surface so the
	   intensity stays consistent across status colours. */
	.badge {
		display: inline-flex;
		align-items: center;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0.1rem 0.5rem;
		border-radius: var(--radius-pill);
		font-weight: 500;
		white-space: nowrap;
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

	.risk-based {
		margin-top: 0.75rem;
	}
	.risk-based > summary {
		cursor: pointer;
		font-size: var(--text-sm);
		color: var(--text-muted);
		padding: 0.35rem 0;
		user-select: none;
	}
	.risk-based[open] > summary {
		margin-bottom: 0.5rem;
	}

	.history-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.75rem;
		margin-bottom: 0.5rem;
	}
	.filter-input {
		padding: 0.3rem 0.5rem;
		background: var(--surface-raised);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		max-width: 240px;
	}

	.table-scroll {
		width: 100%;
		overflow-x: auto;
		-webkit-overflow-scrolling: touch;
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
	td.row-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.25rem;
	}
	a.link {
		color: var(--text-primary);
		text-decoration: none;
		border-bottom: 1px dotted var(--border-default);
	}
	a.link:hover {
		color: var(--accent-hover);
		border-bottom-color: var(--text-muted);
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-base);
		padding: 2rem 1rem;
		text-align: center;
	}
	.empty.small {
		padding: 0.75rem 0;
		font-size: var(--text-sm);
	}
	.msg {
		font-size: var(--text-sm);
		padding: 0.4rem 0.6rem;
		border-radius: 0.3rem;
		margin-bottom: 0.75rem;
	}
	.msg.error {
		background: rgba(204, 102, 102, 0.1);
		color: #e88;
	}

	/* Light theme — remap the dark-tuned status badge / chip colors for white
	   surfaces. The hsla badge backgrounds re-tint fine over light; only the
	   pastel text needs darkening. Dark rules above are untouched. */
	:global(:root[data-theme='light']) .btn.primary {
		border-color: #2563b0;
		color: #2563b0;
	}
	:global(:root[data-theme='light']) .btn.danger:hover:not(:disabled) { color: #c0271d; }
	:global(:root[data-theme='light']) .card:hover { border-color: var(--border-default); }
	:global(:root[data-theme='light']) .badge.status-overdue,
	:global(:root[data-theme='light']) .badge.status-expired { color: #c0271d; }
	:global(:root[data-theme='light']) .badge.status-due_soon { color: #946a00; }
	:global(:root[data-theme='light']) .badge.status-series_incomplete { color: #7c3aed; }
	:global(:root[data-theme='light']) .badge.status-up_to_date { color: #15803d; }
	:global(:root[data-theme='light']) .msg.error { color: #c0271d; }
</style>
