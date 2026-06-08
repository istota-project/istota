<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		createDiagnosis,
		deleteDiagnosis,
		listDiagnoses,
		listEncounters,
		updateDiagnosis,
		type Diagnosis,
		type Encounter,
	} from '$lib/api';

	let loading = $state(true);
	let error = $state('');
	let diagnoses: Diagnosis[] = $state([]);
	let encounters: Encounter[] = $state([]);

	let showResolved = $state(false);

	// Add form
	let formOpen = $state(false);
	let formName = $state('');
	let formStatus = $state<'active' | 'chronic' | 'resolved'>('active');
	let formIcd10 = $state('');
	let formDateDiagnosed = $state(new Date().toISOString().slice(0, 10));
	let formDateResolved = $state('');
	let formEncounterId = $state<string>('');
	let formSeverity = $state<'' | 'mild' | 'moderate' | 'severe'>('');
	let formNotes = $state('');
	let saving = $state(false);
	let formError = $state('');
	let deleteTarget: Diagnosis | null = $state(null);

	async function load() {
		loading = true;
		error = '';
		try {
			const [allResp, encResp] = await Promise.all([
				listDiagnoses({ status: 'all', limit: 500 }),
				listEncounters({ limit: 100 }),
			]);
			diagnoses = allResp.diagnoses;
			encounters = encResp.encounters;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load diagnoses';
		} finally {
			loading = false;
		}
	}

	const active = $derived(diagnoses.filter((d) => d.status === 'active'));
	const chronic = $derived(diagnoses.filter((d) => d.status === 'chronic'));
	const resolved = $derived(diagnoses.filter((d) => d.status === 'resolved'));

	async function submit(e: Event) {
		e.preventDefault();
		formError = '';
		saving = true;
		try {
			await createDiagnosis({
				name: formName,
				status: formStatus,
				icd10: formIcd10 || undefined,
				date_diagnosed: formDateDiagnosed || undefined,
				date_resolved: formDateResolved || undefined,
				encounter_id: formEncounterId ? Number(formEncounterId) : undefined,
				severity: formSeverity || undefined,
				notes: formNotes || undefined,
			});
			formName = '';
			formIcd10 = '';
			formNotes = '';
			formEncounterId = '';
			formSeverity = '';
			formOpen = false;
			await load();
		} catch (e) {
			formError = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}

	async function resolveOne(d: Diagnosis) {
		try {
			await updateDiagnosis(d.id, {
				status: 'resolved',
				date_resolved: new Date().toISOString().slice(0, 10),
			});
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to resolve';
		}
	}

	async function reactivate(d: Diagnosis) {
		try {
			await updateDiagnosis(d.id, { status: 'active', date_resolved: null });
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to reactivate';
		}
	}

	async function confirmDeletion() {
		if (!deleteTarget) return;
		const id = deleteTarget.id;
		deleteTarget = null;
		try {
			await deleteDiagnosis(id);
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete';
		}
	}

	function formatDate(iso: string | null): string {
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

	function encounterLabel(id: number | null): string {
		if (!id) return '';
		const e = encounters.find((x) => x.id === id);
		if (!e) return `#${id}`;
		return `${formatDate(e.encounter_date)} · ${e.encounter_type}`;
	}

	onMount(load);
</script>

<div class="header">
	<div>
		<a class="back" href="{base}/health/history">← Medical history</a>
		<h1>Conditions</h1>
	</div>
	<button class="btn" type="button" onclick={() => (formOpen = !formOpen)}>
		{formOpen ? 'Cancel' : '+ Add diagnosis'}
	</button>
</div>

{#if formOpen}
	<form class="form" onsubmit={submit}>
		<div class="row">
			<label class="full">
				<span>Name *</span>
				<input type="text" bind:value={formName} required placeholder="e.g. Hypertension" />
			</label>
			<label>
				<span>Status</span>
				<select bind:value={formStatus}>
					<option value="active">Active</option>
					<option value="chronic">Chronic</option>
					<option value="resolved">Resolved</option>
				</select>
			</label>
			<label>
				<span>ICD-10</span>
				<input type="text" bind:value={formIcd10} placeholder="K64.0" />
			</label>
			<label>
				<span>Severity</span>
				<select bind:value={formSeverity}>
					<option value="">—</option>
					<option value="mild">Mild</option>
					<option value="moderate">Moderate</option>
					<option value="severe">Severe</option>
				</select>
			</label>
			<label>
				<span>Date diagnosed</span>
				<input type="date" bind:value={formDateDiagnosed} />
			</label>
			{#if formStatus === 'resolved'}
				<label>
					<span>Date resolved</span>
					<input type="date" bind:value={formDateResolved} />
				</label>
			{/if}
			<label>
				<span>Linked encounter</span>
				<select bind:value={formEncounterId}>
					<option value="">—</option>
					{#each encounters as e (e.id)}
						<option value={String(e.id)}>{formatDate(e.encounter_date)} · {e.encounter_type}</option>
					{/each}
				</select>
			</label>
		</div>
		<label class="full">
			<span>Notes</span>
			<textarea bind:value={formNotes} rows="3"></textarea>
		</label>
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
	<section>
		<h2>Active <span class="count">{active.length}</span></h2>
		{#if active.length === 0}
			<div class="empty small">No active conditions on file.</div>
		{:else}
			<ul class="list">
				{#each active as d (d.id)}
					<li>
						<div class="d-row">
							<div class="d-main">
								<span class="name">{d.name}</span>
								{#if d.icd10}<span class="icd">{d.icd10}</span>{/if}
								{#if d.severity}<span class="sev sev-{d.severity}">{d.severity}</span>{/if}
							</div>
							<div class="d-meta">
								{#if d.date_diagnosed}<span>Dx {formatDate(d.date_diagnosed)}</span>{/if}
								{#if d.encounter_id}
									<a href="{base}/health/history/encounter?id={d.encounter_id}" class="enc">
										{encounterLabel(d.encounter_id)}
									</a>
								{/if}
							</div>
							<div class="d-actions">
								<button class="btn small" onclick={() => resolveOne(d)}>Resolve</button>
								<button class="btn small danger" onclick={() => (deleteTarget = d)}>Delete</button>
							</div>
						</div>
						{#if d.notes}<p class="notes">{d.notes}</p>{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<section>
		<h2>Chronic <span class="count">{chronic.length}</span></h2>
		{#if chronic.length === 0}
			<div class="empty small">No chronic conditions on file.</div>
		{:else}
			<ul class="list">
				{#each chronic as d (d.id)}
					<li>
						<div class="d-row">
							<div class="d-main">
								<span class="name">{d.name}</span>
								{#if d.icd10}<span class="icd">{d.icd10}</span>{/if}
							</div>
							<div class="d-meta">
								{#if d.date_diagnosed}<span>Dx {formatDate(d.date_diagnosed)}</span>{/if}
								{#if d.encounter_id}
									<a href="{base}/health/history/encounter?id={d.encounter_id}" class="enc">
										{encounterLabel(d.encounter_id)}
									</a>
								{/if}
							</div>
							<div class="d-actions">
								<button class="btn small danger" onclick={() => (deleteTarget = d)}>Delete</button>
							</div>
						</div>
						{#if d.notes}<p class="notes">{d.notes}</p>{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<section>
		<h2>
			Resolved <span class="count">{resolved.length}</span>
			<button class="toggle" type="button" onclick={() => (showResolved = !showResolved)}>
				{showResolved ? 'hide' : 'show'}
			</button>
		</h2>
		{#if showResolved}
			{#if resolved.length === 0}
				<div class="empty small">Nothing resolved yet.</div>
			{:else}
				<ul class="list resolved">
					{#each resolved as d (d.id)}
						<li>
							<div class="d-row">
								<div class="d-main">
									<span class="name">{d.name}</span>
								</div>
								<div class="d-meta">
									{#if d.date_resolved}<span>resolved {formatDate(d.date_resolved)}</span>{/if}
								</div>
								<div class="d-actions">
									<button class="btn small" onclick={() => reactivate(d)}>Reactivate</button>
									<button class="btn small danger" onclick={() => (deleteTarget = d)}>Delete</button>
								</div>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}
	</section>
{/if}

{#if deleteTarget}
	<div class="modal-backdrop" onclick={() => (deleteTarget = null)} role="presentation">
		<div class="modal" onclick={(e) => e.stopPropagation()} role="presentation">
			<h2>Delete diagnosis?</h2>
			<p>Permanently removes <strong>{deleteTarget.name}</strong> from your history. This cannot be undone.</p>
			<div class="modal-actions">
				<button class="btn" type="button" onclick={() => (deleteTarget = null)}>Cancel</button>
				<button class="btn danger" type="button" onclick={confirmDeletion}>Delete</button>
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
		margin: 1.5rem 0 0.5rem;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.count {
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-weight: 400;
		letter-spacing: 0;
		text-transform: none;
	}
	.toggle {
		margin-left: auto;
		font-size: var(--text-xs);
		background: transparent;
		border: none;
		color: var(--text-muted);
		cursor: pointer;
		padding: 0;
		text-transform: none;
		letter-spacing: 0;
	}
	.toggle:hover { color: var(--text-primary); text-decoration: underline; }

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
	.btn.small { padding: 0.15rem 0.5rem; font-size: var(--text-xs); }
	.btn.primary { border-color: #7aa3d8; color: #7aa3d8; }
	.btn.danger { border-color: #c66; color: #c66; }

	.form {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.85rem 1rem;
		margin-bottom: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.65rem;
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
	.form input, .form select, .form textarea {
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
	}

	.list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.list li {
		padding: 0.7rem 0.9rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
	}
	.list.resolved li {
		opacity: 0.7;
	}
	.d-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.75rem;
		flex-wrap: wrap;
	}
	.d-main {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		min-width: 0;
	}
	.name { font-weight: 500; color: var(--text-primary); }
	.icd {
		font-size: var(--text-xs);
		color: var(--text-muted);
		background: var(--surface-raised);
		padding: 0.05rem 0.45rem;
		border-radius: 0.25rem;
		font-family: var(--font-mono, ui-monospace, "SF Mono", monospace);
	}
	.sev {
		display: inline-flex;
		align-items: center;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0.05rem 0.5rem;
		border-radius: var(--radius-pill);
		font-weight: 500;
	}
	.sev-mild     { background: hsla(145, 40%, 55%, 0.18); color: #9bd6a6; }
	.sev-moderate { background: hsla(35,  60%, 60%, 0.22); color: #e6b96b; }
	.sev-severe   { background: hsla(0,   60%, 55%, 0.28); color: #ff9d96; }
	.d-meta {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
		flex-wrap: wrap;
	}
	.d-meta .enc {
		color: var(--text-muted);
		text-decoration: none;
	}
	.d-meta .enc:hover { color: #7aa3d8; text-decoration: underline; }
	.d-actions { display: flex; gap: 0.3rem; flex: 0 0 auto; }
	.notes {
		margin: 0.5rem 0 0;
		font-size: var(--text-sm);
		white-space: pre-wrap;
		color: var(--text-muted);
		line-height: 1.45;
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
		max-width: 26rem;
	}
	.modal h2 {
		font-size: var(--text-base);
		font-weight: 500;
		margin: 0 0 0.5rem;
		text-transform: none;
		letter-spacing: 0;
		color: var(--text-primary);
		display: block;
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
	:global(:root[data-theme='light']) .sev-mild { color: #15803d; }
	:global(:root[data-theme='light']) .sev-moderate { color: #946a00; }
	:global(:root[data-theme='light']) .sev-severe { color: #c0271d; }
	:global(:root[data-theme='light']) .d-meta .enc:hover { color: #2563b0; }
	:global(:root[data-theme='light']) .msg.error { color: #c0271d; }
</style>
