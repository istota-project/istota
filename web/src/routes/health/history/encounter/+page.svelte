<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { base } from '$app/paths';
	import {
		deleteEncounter,
		getEncounter,
		updateEncounter,
		type Diagnosis,
		type Encounter,
		type HealthPanel,
	} from '$lib/api';

	let loading = $state(true);
	let error = $state('');
	let saving = $state(false);
	let encounter: Encounter | null = $state(null);
	let diagnoses: Diagnosis[] = $state([]);
	let panels: HealthPanel[] = $state([]);

	let editing = $state(false);
	let form: Partial<Encounter> = $state({});
	let confirmDelete = $state(false);

	const CANONICAL_TYPES = [
		'visit',
		'procedure',
		'screening',
		'hospitalization',
		'er',
		'telehealth',
		'imaging',
		'dental',
		'other',
	] as const;

	function typeLabel(t: string | null | undefined): string {
		if (!t) return '';
		const m: Record<string, string> = {
			visit: 'Visit',
			procedure: 'Procedure',
			screening: 'Screening',
			hospitalization: 'Hospital',
			er: 'ER',
			telehealth: 'Telehealth',
			imaging: 'Imaging',
			dental: 'Dental',
			other: 'Other',
		};
		return m[t] ?? t.charAt(0).toUpperCase() + t.slice(1);
	}

	// Make sure the current encounter_type is always selectable so Svelte's
	// bind doesn't silently switch a free-text type to the first option.
	const editTypeOptions = $derived.by(() => {
		const current = (form.encounter_type ?? '') as string;
		const opts = [...CANONICAL_TYPES] as string[];
		if (current && !opts.includes(current)) opts.unshift(current);
		return opts;
	});

	const encounterId = $derived.by(() => {
		const raw = page.url.searchParams.get('id');
		const n = raw ? Number(raw) : NaN;
		return Number.isFinite(n) ? n : null;
	});

	async function load() {
		if (encounterId === null) {
			error = 'Missing encounter id';
			loading = false;
			return;
		}
		loading = true;
		error = '';
		try {
			const resp = await getEncounter(encounterId);
			encounter = resp.encounter;
			diagnoses = resp.diagnoses;
			panels = resp.panels;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load encounter';
		} finally {
			loading = false;
		}
	}

	function startEdit() {
		if (!encounter) return;
		form = { ...encounter };
		editing = true;
	}

	async function save(e: Event) {
		e.preventDefault();
		if (encounterId === null) return;
		saving = true;
		try {
			await updateEncounter(encounterId, form);
			editing = false;
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}

	async function destroy() {
		if (encounterId === null) return;
		try {
			await deleteEncounter(encounterId);
			goto(`${base}/health/history`);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete';
			confirmDelete = false;
		}
	}

	function formatDate(iso: string | null | undefined): string {
		if (!iso) return '';
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

	onMount(load);
	$effect(() => {
		encounterId;
		load();
	});
</script>

<div class="header">
	<div>
		<a class="back" href="{base}/health/history">← Medical history</a>
		<h1>Encounter</h1>
	</div>
	{#if encounter && !editing}
		<div class="actions">
			<button class="btn" type="button" onclick={startEdit}>Edit</button>
			<button class="btn danger" type="button" onclick={() => (confirmDelete = true)}>Delete</button>
		</div>
	{/if}
</div>

{#if loading}
	<div class="loading">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else if encounter}
	{#if editing}
		<form class="form" onsubmit={save}>
			<div class="row">
				<label>
					<span>Date</span>
					<input type="date" bind:value={form.encounter_date} required />
				</label>
				<label>
					<span>Type</span>
					<select bind:value={form.encounter_type}>
						{#each editTypeOptions as t (t)}
							<option value={t}>{typeLabel(t)}</option>
						{/each}
					</select>
				</label>
				<label>
					<span>Provider</span>
					<input type="text" bind:value={form.provider} />
				</label>
				<label>
					<span>Facility</span>
					<input type="text" bind:value={form.facility} />
				</label>
				<label>
					<span>Specialty</span>
					<input type="text" bind:value={form.specialty} />
				</label>
			</div>
			<label class="full">
				<span>Reason</span>
				<input type="text" bind:value={form.reason} />
			</label>
			<label class="full">
				<span>Notes</span>
				<textarea bind:value={form.notes} rows="5"></textarea>
			</label>
			<div class="form-actions">
				<button type="button" class="btn" onclick={() => (editing = false)}>Cancel</button>
				<button type="submit" class="btn primary" disabled={saving}>
					{saving ? 'Saving…' : 'Save'}
				</button>
			</div>
		</form>
	{:else}
		<section class="meta">
			<dl>
				<div><dt>Date</dt><dd>{formatDate(encounter.encounter_date)}</dd></div>
				<div><dt>Type</dt><dd>{typeLabel(encounter.encounter_type)}</dd></div>
				{#if encounter.provider}
					<div><dt>Provider</dt><dd>{encounter.provider}</dd></div>
				{/if}
				{#if encounter.facility}
					<div><dt>Facility</dt><dd>{encounter.facility}</dd></div>
				{/if}
				{#if encounter.specialty}
					<div><dt>Specialty</dt><dd>{encounter.specialty}</dd></div>
				{/if}
				{#if encounter.reason}
					<div><dt>Reason</dt><dd>{encounter.reason}</dd></div>
				{/if}
			</dl>
		</section>

		{#if encounter.notes}
			<section class="notes">
				<h2>Notes</h2>
				<p>{encounter.notes}</p>
			</section>
		{/if}
	{/if}

	<section class="related">
		<h2>Linked diagnoses</h2>
		{#if diagnoses.length === 0}
			<div class="empty small">None.</div>
		{:else}
			<ul>
				{#each diagnoses as d (d.id)}
					<li>
						<a href="{base}/health/history/diagnoses">
							<span class="name">{d.name}</span>
							<span class="badge status-{d.status}">{d.status}</span>
						</a>
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<section class="related">
		<h2>Linked panels</h2>
		{#if panels.length === 0}
			<div class="empty small">None.</div>
		{:else}
			<ul>
				{#each panels as p (p.id)}
					<li>
						<a href="{base}/health/bloodwork/panel?id={p.id}">
							<span>{formatDate(p.drawn_at)}</span>
							<span class="muted">{p.lab_name || ''}</span>
							<span class="muted">{p.biomarker_count} marker{p.biomarker_count === 1 ? '' : 's'}</span>
						</a>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
{/if}

{#if confirmDelete}
	<div class="modal-backdrop" onclick={() => (confirmDelete = false)} role="presentation">
		<div class="modal" onclick={(e) => e.stopPropagation()} role="presentation">
			<h2>Delete this encounter?</h2>
			<p>Linked panels and diagnoses keep their data but lose the link.</p>
			<div class="modal-actions">
				<button class="btn" type="button" onclick={() => (confirmDelete = false)}>Cancel</button>
				<button class="btn danger" type="button" onclick={destroy}>Delete</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1rem;
		margin-bottom: 1rem;
	}
	.back {
		display: inline-block;
		font-size: var(--text-xs);
		color: var(--text-muted);
		text-decoration: none;
		margin-bottom: 0.25rem;
	}
	.back:hover { text-decoration: underline; }
	h1 { font-size: var(--text-lg); font-weight: 500; margin: 0; }
	h2 {
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
		font-weight: 500;
		margin: 1.25rem 0 0.5rem;
	}

	.actions { display: flex; gap: 0.5rem; }
	.btn {
		padding: 0.35rem 0.75rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		cursor: pointer;
		line-height: 1.2;
	}
	.btn:disabled { opacity: 0.6; cursor: not-allowed; }
	.btn:hover:not(:disabled) { background: var(--surface-raised); }
	.btn.primary { border-color: #7aa3d8; color: #7aa3d8; }
	.btn.danger { border-color: #c66; color: #c66; }

	.meta {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.85rem 1rem;
	}
	.meta dl {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
		gap: 0.6rem 1.5rem;
		margin: 0;
	}
	.meta dt {
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
		margin-bottom: 0.15rem;
	}
	.meta dd {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--text-primary);
	}

	.notes {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.85rem 1rem;
		margin-top: 1rem;
	}
	.notes h2 { margin-top: 0; }
	.notes p {
		white-space: pre-wrap;
		line-height: 1.5;
		margin: 0;
		font-size: var(--text-sm);
		color: var(--text-primary);
	}

	.related ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.related li a {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1rem;
		padding: 0.5rem 0.75rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		text-decoration: none;
		color: var(--text-primary);
		font-size: var(--text-sm);
	}
	.related li a:hover { border-color: #555; }
	.related .name { font-weight: 500; }
	.related .muted { color: var(--text-muted); font-size: var(--text-xs); }

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
	.badge.status-active   { background: hsla(0,   55%, 60%, 0.22); color: #f0a09c; }
	.badge.status-chronic  { background: hsla(35,  60%, 60%, 0.22); color: #e6b96b; }
	.badge.status-resolved { background: hsla(145, 40%, 55%, 0.18); color: #9bd6a6; }

	.form {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.85rem 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.65rem;
		margin-bottom: 1rem;
	}
	.form .row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.65rem;
	}
	.form label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--text-sm);
		min-width: 0;
	}
	.form label > span {
		color: var(--text-muted);
		font-size: var(--text-xs);
	}
	.form label.full { grid-column: 1 / -1; }
	.form input,
	.form select,
	.form textarea {
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
	.form textarea { resize: vertical; font-family: inherit; }
	.form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-base);
		padding: 2rem 1rem;
		text-align: center;
	}
	.empty.small {
		padding: 0.5rem 0;
		font-size: var(--text-sm);
	}
	.msg {
		font-size: var(--text-sm);
		padding: 0.4rem 0.6rem;
		border-radius: 0.3rem;
		margin-bottom: 0.75rem;
	}
	.msg.error { background: rgba(204, 102, 102, 0.1); color: #f0a; }

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
		max-width: 24rem;
	}
	.modal h2 {
		font-size: var(--text-base);
		font-weight: 500;
		margin: 0 0 0.5rem;
		text-transform: none;
		letter-spacing: 0;
		color: var(--text-primary);
	}
	.modal p {
		font-size: var(--text-sm);
		color: var(--text-muted);
		margin: 0 0 1rem;
	}
	.modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
	}

	/* Light theme overrides — dark rules above untouched. */
	:global(:root[data-theme='light']) .btn.primary { border-color: #2563b0; color: #2563b0; }
	:global(:root[data-theme='light']) .btn.danger { border-color: #c0271d; color: #c0271d; }
	:global(:root[data-theme='light']) .related li a:hover { border-color: var(--border-default); }
	:global(:root[data-theme='light']) .badge.status-active { color: #c0271d; }
	:global(:root[data-theme='light']) .badge.status-chronic { color: #946a00; }
	:global(:root[data-theme='light']) .badge.status-resolved { color: #15803d; }
	:global(:root[data-theme='light']) .msg.error { color: #c0271d; }
</style>
