<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		createDiagnosis,
		createEncounter,
		listDiagnoses,
		listEncounters,
		type Diagnosis,
		type Encounter,
	} from '$lib/api';

	// Suggested types — the server accepts any free-text encounter_type, so
	// these are just defaults for the dropdowns. Unknown types from the API
	// flow through `typeLabel` and a generic badge style.
	const ENCOUNTER_TYPES = [
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

	let loading = $state(true);
	let error = $state('');
	let encounters: Encounter[] = $state([]);
	let active: Diagnosis[] = $state([]);
	let chronic: Diagnosis[] = $state([]);

	let typeFilter = $state('');
	let sinceFilter = $state('');
	let untilFilter = $state('');

	// Quick-add encounter form
	let formOpen = $state(false);
	let formDate = $state(new Date().toISOString().slice(0, 10));
	let formType = $state('visit');
	let formProvider = $state('');
	let formFacility = $state('');
	let formSpecialty = $state('');
	let formReason = $state('');
	let formNotes = $state('');
	let saving = $state(false);
	let formError = $state('');

	async function load() {
		loading = true;
		error = '';
		try {
			const [encResp, actResp, chrResp] = await Promise.all([
				listEncounters({
					type: typeFilter || undefined,
					since: sinceFilter || undefined,
					until: untilFilter || undefined,
					limit: 200,
				}),
				listDiagnoses({ status: 'active', limit: 200 }),
				listDiagnoses({ status: 'chronic', limit: 200 }),
			]);
			encounters = encResp.encounters;
			active = actResp.diagnoses;
			chronic = chrResp.diagnoses;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load history';
		} finally {
			loading = false;
		}
	}

	async function submit(e: Event) {
		e.preventDefault();
		formError = '';
		saving = true;
		try {
			await createEncounter({
				encounter_date: formDate,
				encounter_type: formType,
				provider: formProvider || undefined,
				facility: formFacility || undefined,
				specialty: formSpecialty || undefined,
				reason: formReason || undefined,
				notes: formNotes || undefined,
			});
			formProvider = '';
			formFacility = '';
			formSpecialty = '';
			formReason = '';
			formNotes = '';
			formOpen = false;
			await load();
		} catch (e) {
			formError = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}

	function formatDate(iso: string): string {
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

	function typeLabel(t: string): string {
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
		if (m[t]) return m[t];
		// Unknown free-text type: title-case the first segment for display.
		if (!t) return 'Unknown';
		return t.charAt(0).toUpperCase() + t.slice(1);
	}

	function typeBadgeClass(t: string): string {
		return (ENCOUNTER_TYPES as readonly string[]).includes(t)
			? `type-${t}`
			: 'type-other';
	}

	// All types observed in the data plus the canonical list — used so the
	// <select> never silently switches to a value not in its options.
	const allTypes = $derived(
		Array.from(
			new Set<string>([
				...ENCOUNTER_TYPES,
				...encounters.map((e) => e.encounter_type).filter(Boolean),
			]),
		),
	);

	onMount(load);
</script>

<div class="header">
	<h1>Medical history</h1>
	<div class="actions">
		<button class="btn" type="button" onclick={() => (formOpen = !formOpen)}>
			{formOpen ? 'Cancel' : '+ Add encounter'}
		</button>
		<a class="btn" href="{base}/health/history/import">Import</a>
		<a class="btn" href="{base}/health/history/diagnoses">Conditions</a>
	</div>
</div>

{#if formOpen}
	<form class="quick-form" onsubmit={submit}>
		<div class="row">
			<label>
				<span>Date</span>
				<input type="date" bind:value={formDate} required />
			</label>
			<label>
				<span>Type</span>
				<select bind:value={formType}>
					{#each ENCOUNTER_TYPES as t}
						<option value={t}>{typeLabel(t)}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>Provider</span>
				<input type="text" bind:value={formProvider} placeholder="Dr. Smith" />
			</label>
			<label>
				<span>Facility</span>
				<input type="text" bind:value={formFacility} placeholder="Kaiser Sunset" />
			</label>
			<label>
				<span>Specialty</span>
				<input type="text" bind:value={formSpecialty} placeholder="cardiology" />
			</label>
		</div>
		<label class="full">
			<span>Reason</span>
			<input type="text" bind:value={formReason} placeholder="Chief complaint or reason for visit" />
		</label>
		<label class="full">
			<span>Notes</span>
			<textarea bind:value={formNotes} rows="3" placeholder="Findings, follow-ups, …"></textarea>
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

<div class="filter-bar">
	<label>
		<span>Type</span>
		<select bind:value={typeFilter} onchange={load}>
			<option value="">All types</option>
			{#each allTypes as t (t)}
				<option value={t}>{typeLabel(t)}</option>
			{/each}
		</select>
	</label>
	<label>
		<span>Since</span>
		<input type="date" bind:value={sinceFilter} onchange={load} />
	</label>
	<label>
		<span>Until</span>
		<input type="date" bind:value={untilFilter} onchange={load} />
	</label>
</div>

{#if loading}
	<div class="loading">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else}
	<div class="layout">
		<section class="timeline">
			{#if encounters.length === 0}
				<div class="empty">
					No encounters yet. Use <strong>+ Add encounter</strong> above to record one.
				</div>
			{:else}
				<ul>
					{#each encounters as e (e.id)}
						<li>
							<a class="card" href="{base}/health/history/encounter?id={e.id}">
								<div class="card-head">
									<span class="badge {typeBadgeClass(e.encounter_type)}">{typeLabel(e.encounter_type)}</span>
									<span class="date">{formatDate(e.encounter_date)}</span>
								</div>
								<div class="card-body">
									{#if e.provider || e.facility}
										<div class="who">
											{e.provider || ''}{e.provider && e.facility ? ' · ' : ''}{e.facility || ''}
										</div>
									{/if}
									{#if e.specialty}
										<div class="muted">{e.specialty}</div>
									{/if}
									{#if e.reason}
										<div class="reason">{e.reason}</div>
									{/if}
								</div>
							</a>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<aside class="sidebar">
			<h2>Active conditions</h2>
			{#if active.length === 0 && chronic.length === 0}
				<div class="empty small">No active conditions on file.</div>
			{:else}
				{#if active.length > 0}
					<ul class="conditions">
						{#each active as d (d.id)}
							<li>
								<a href="{base}/health/history/diagnoses">
									<span class="name">{d.name}</span>
									{#if d.severity}
										<span class="severity sev-{d.severity}">{d.severity}</span>
									{/if}
								</a>
							</li>
						{/each}
					</ul>
				{/if}
				{#if chronic.length > 0}
					<h3>Chronic</h3>
					<ul class="conditions">
						{#each chronic as d (d.id)}
							<li>
								<a href="{base}/health/history/diagnoses">
									<span class="name">{d.name}</span>
								</a>
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		</aside>
	</div>
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
		line-height: 1.2;
	}
	.btn:disabled { opacity: 0.6; cursor: not-allowed; }
	.btn:hover:not(:disabled) { background: var(--surface-raised); }
	.btn.primary { border-color: #7aa3d8; color: #7aa3d8; }

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
	.form-actions {
		display: flex;
		justify-content: flex-end;
	}

	.filter-bar {
		display: flex;
		gap: 0.75rem;
		margin-bottom: 1rem;
		flex-wrap: wrap;
		align-items: flex-end;
	}
	.filter-bar label {
		display: flex;
		flex-direction: column;
		font-size: var(--text-sm);
		gap: 0.15rem;
		min-width: 0;
	}
	.filter-bar label > span {
		color: var(--text-dim);
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.filter-bar input,
	.filter-bar select {
		padding: 0.3rem 0.5rem;
		background: var(--surface-raised);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
	}

	.layout {
		display: grid;
		grid-template-columns: 1fr 280px;
		gap: 1.25rem;
	}
	@media (max-width: 768px) {
		.layout {
			grid-template-columns: 1fr;
		}
	}

	.timeline ul,
	.conditions {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.card {
		display: block;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.7rem 0.9rem;
		text-decoration: none;
		color: var(--text-primary);
	}
	.card:hover { border-color: #555; }
	.card-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.35rem;
	}
	.card-body .who {
		font-weight: 500;
		font-size: var(--text-sm);
	}
	.card-body .muted {
		font-size: var(--text-xs);
		color: var(--text-muted);
		text-transform: lowercase;
	}
	.card-body .reason {
		margin-top: 0.25rem;
		font-size: var(--text-sm);
		color: var(--text-muted);
	}

	/* Badge palette tuned for the dark surface: each canonical encounter
	   type gets its own hue via HSLA so they share intensity. type-other
	   is the catch-all for unknown free-text types. */
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
	.badge.type-visit          { background: hsla(210, 45%, 65%, 0.22); color: #b6ccea; }
	.badge.type-procedure      { background: hsla(35,  60%, 60%, 0.22); color: #e6b96b; }
	.badge.type-screening      { background: hsla(195, 50%, 60%, 0.22); color: #9cd5ea; }
	.badge.type-hospitalization{ background: hsla(0,   55%, 60%, 0.25); color: #f0a09c; }
	.badge.type-er             { background: hsla(0,   60%, 55%, 0.32); color: #ff9d96; }
	.badge.type-telehealth     { background: hsla(145, 40%, 55%, 0.22); color: #9bd6a6; }
	.badge.type-imaging        { background: hsla(280, 45%, 65%, 0.22); color: #d0aeec; }
	.badge.type-dental         { background: hsla(185, 45%, 60%, 0.22); color: #95d2dc; }
	.badge.type-other          { background: hsla(220, 8%,  60%, 0.18); color: var(--text-muted); }

	.date {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.sidebar {
		border-left: 1px solid var(--border-subtle);
		padding-left: 1.25rem;
	}
	@media (max-width: 768px) {
		.sidebar {
			border-left: none;
			border-top: 1px solid var(--border-subtle);
			padding-left: 0;
			padding-top: 1rem;
		}
	}
	.sidebar h2 {
		margin: 0 0 0.5rem;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
		font-weight: 500;
	}
	.sidebar h3 {
		margin: 0.85rem 0 0.4rem;
		font-size: var(--text-xs);
		font-weight: 500;
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.conditions li a {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0.35rem 0.55rem;
		border-radius: 0.3rem;
		text-decoration: none;
		color: var(--text-primary);
		font-size: var(--text-sm);
	}
	.conditions li a:hover { background: var(--surface-raised); }
	.conditions .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.severity {
		font-size: var(--text-xs);
		text-transform: uppercase;
		padding: 0 0.4rem;
		border-radius: var(--radius-pill);
		flex: 0 0 auto;
	}
	.severity.sev-mild     { background: hsla(145, 40%, 55%, 0.18); color: #9bd6a6; }
	.severity.sev-moderate { background: hsla(35,  60%, 60%, 0.22); color: #e6b96b; }
	.severity.sev-severe   { background: hsla(0,   60%, 55%, 0.28); color: #ff9d96; }

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
	.msg.error { background: rgba(204, 102, 102, 0.1); color: #f0a; }
</style>
