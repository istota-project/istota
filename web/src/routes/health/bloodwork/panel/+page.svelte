<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import {
		deleteHealthPanel,
		getHealthPanel,
		healthPanelSourceUrl,
		listEncounters,
		saveHealthBiomarkers,
		updateHealthPanel,
		type Biomarker,
		type Encounter,
		type HealthPanel,
	} from '$lib/api';

	// Read the panel id from ?id=… so the page is statically prerenderable
	// under adapter-static; the actual lookup happens client-side.
	let id = $derived(Number(page.url.searchParams.get('id') ?? 0));

	let loading = $state(true);
	let error = $state('');
	let info = $state('');
	let panel: HealthPanel | null = $state(null);
	let biomarkers: Biomarker[] = $state([]);
	let source = $state({ available: false, mime: null as string | null });

	let editing = $state(false);
	let saving = $state(false);
	let confirmDelete = $state(false);

	// Header field edits — populated when entering edit mode so Cancel can
	// discard cleanly without re-fetching.
	let editDrawnAt = $state('');
	let editLabName = $state('');
	let editPanelType = $state('');
	// `''` means "no link"; numeric string is an encounter id. <select> binds
	// to strings, so we round-trip through string for clean change detection.
	let editEncounterId = $state('');

	let encounters: Encounter[] = $state([]);
	let encountersLoaded = $state(false);

	function startEditing() {
		if (!panel) return;
		// Truncate any time-of-day portion for the <input type="date">.
		editDrawnAt = (panel.drawn_at || '').slice(0, 10);
		editLabName = panel.lab_name || '';
		editPanelType = panel.panel_type || '';
		editEncounterId = panel.encounter_id == null ? '' : String(panel.encounter_id);
		editing = true;
		loadEncounters();
	}

	async function loadEncounters() {
		if (encountersLoaded) return;
		try {
			const resp = await listEncounters({ limit: 200 });
			encounters = resp.encounters;
			encountersLoaded = true;
		} catch {
			// Non-fatal; the select will just be empty.
		}
	}

	function encounterLabel(e: Encounter): string {
		const parts = [e.encounter_date, e.encounter_type];
		if (e.provider) parts.push(e.provider);
		else if (e.facility) parts.push(e.facility);
		return parts.join(' · ');
	}

	const linkedEncounter: Encounter | null = $derived.by(() => {
		const p = panel;
		if (!p || p.encounter_id == null) return null;
		return encounters.find((e) => e.id === p.encounter_id) ?? null;
	});

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await getHealthPanel(id);
			panel = resp.panel;
			biomarkers = [...resp.biomarkers];
			source = resp.source;
			// Fetch encounters in the background so the read-mode label can
			// resolve. Cheap; the list is small.
			void loadEncounters();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load panel';
		} finally {
			loading = false;
		}
	}

	async function save(confirmDraft: boolean) {
		if (!panel) return;
		saving = true;
		error = '';
		info = '';
		try {
			// Only send header fields that actually changed; the API treats
			// an explicit empty string as "set to null", which we want for
			// the user clearing lab/panel_type but not as an accidental wipe.
			const headerPatch: Record<string, unknown> = {};
			if (editDrawnAt && editDrawnAt !== (panel.drawn_at || '').slice(0, 10)) {
				headerPatch.drawn_at = editDrawnAt;
			}
			if (editLabName !== (panel.lab_name || '')) {
				headerPatch.lab_name = editLabName;
			}
			if (editPanelType !== (panel.panel_type || '')) {
				headerPatch.panel_type = editPanelType;
			}
			const newEncounterId = editEncounterId === '' ? null : Number(editEncounterId);
			if (newEncounterId !== (panel.encounter_id ?? null)) {
				headerPatch.encounter_id = newEncounterId;
			}
			if (Object.keys(headerPatch).length > 0) {
				await updateHealthPanel(id, headerPatch);
			}

			const payload = biomarkers.map((b) => ({
				name: b.name,
				display_name: b.display_name ?? undefined,
				value: Number(b.value),
				unit: b.unit,
				ref_range_low: b.ref_range_low ?? undefined,
				ref_range_high: b.ref_range_high ?? undefined,
				flag: b.flag ?? undefined,
			}));
			await saveHealthBiomarkers(id, payload, confirmDraft);
			info = confirmDraft ? 'Saved + confirmed.' : 'Saved.';
			editing = false;
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}

	async function confirmDraftOnly() {
		try {
			await updateHealthPanel(id, { draft: false });
			info = 'Panel confirmed.';
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to confirm';
		}
	}

	async function deletePanel() {
		try {
			await deleteHealthPanel(id);
			goto(`${base}/health/bloodwork`);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete';
			confirmDelete = false;
		}
	}

	function addRow() {
		biomarkers = [
			...biomarkers,
			{
				id: -Date.now(),
				panel_id: id,
				name: '',
				display_name: null,
				value: 0,
				unit: '',
				ref_range_low: null,
				ref_range_high: null,
				flag: null,
			},
		];
	}

	function removeRow(index: number) {
		biomarkers = biomarkers.filter((_, i) => i !== index);
	}

	function formatDate(iso: string): string {
		try {
			const d = new Date(iso + (iso.includes('T') ? '' : 'T00:00:00Z'));
			return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
		} catch {
			return iso;
		}
	}

	onMount(load);
</script>

<div class="page">
	{#if loading}
		<div class="empty">Loading…</div>
	{:else if error && !panel}
		<div class="msg error">{error}</div>
	{:else if panel}
		<div class="header">
			<div class="header-meta">
				<a href="{base}/health/bloodwork" class="back">← Bloodwork</a>
				{#if editing}
					<div class="header-edit">
						<label>
							<span>Date drawn</span>
							<input type="date" bind:value={editDrawnAt} />
						</label>
						<label>
							<span>Lab</span>
							<input type="text" bind:value={editLabName} placeholder="Quest, Kaiser, …" />
						</label>
						<label>
							<span>Panel type</span>
							<input type="text" bind:value={editPanelType} placeholder="CBC, CMP, Lipid, …" />
						</label>
						<label class="full-row">
							<span>Linked encounter</span>
							<select bind:value={editEncounterId}>
								<option value="">— Not linked —</option>
								{#each encounters as e (e.id)}
									<option value={String(e.id)}>{encounterLabel(e)}</option>
								{/each}
							</select>
						</label>
					</div>
				{:else}
					<h1>
						{formatDate(panel.drawn_at)}
						<span class="lab">· {panel.lab_name || 'Unknown lab'}</span>
						{#if panel.panel_type}<span class="type">· {panel.panel_type}</span>{/if}
					</h1>
					{#if panel.encounter_id != null}
						<div class="encounter-link">
							<span class="encounter-label">Linked to encounter:</span>
							<a href="{base}/health/history/encounter?id={panel.encounter_id}">
								{linkedEncounter ? encounterLabel(linkedEncounter) : `#${panel.encounter_id}`}
							</a>
						</div>
					{/if}
				{/if}
				{#if panel.draft}<span class="badge draft">DRAFT — review and confirm</span>{/if}
			</div>
			<div class="actions">
				{#if !editing}
					<button class="btn" type="button" onclick={startEditing}>Edit panel</button>
					{#if panel.draft}
						<button class="btn primary" type="button" onclick={confirmDraftOnly}>Confirm</button>
					{/if}
				{:else}
					<button class="btn" type="button" onclick={() => (editing = false)} disabled={saving}>Cancel</button>
					<button class="btn primary" type="button" onclick={() => save(true)} disabled={saving}>
						{saving ? 'Saving…' : 'Save + confirm'}
					</button>
				{/if}
				<button class="btn danger" type="button" onclick={() => (confirmDelete = true)}>Delete</button>
			</div>
		</div>

		{#if info}<div class="msg info">{info}</div>{/if}
		{#if error && panel}<div class="msg error">{error}</div>{/if}

		<div class="split">
			<div class="biomarker-table">
				<table>
					<thead>
						<tr>
							<th>Marker</th>
							<th>Value</th>
							<th>Unit</th>
							<th>Lab range</th>
							<th>Flag</th>
							{#if editing}<th></th>{/if}
						</tr>
					</thead>
					<tbody>
						{#each biomarkers as b, i (b.id)}
							<tr class:flag-row={b.flag}>
								<td>
									{#if editing}
										<input bind:value={b.name} placeholder="Hemoglobin" />
									{:else}
										<a class="marker-link" href="{base}/health/bloodwork/marker?name={encodeURIComponent(b.name)}">
											{b.display_name || b.name}
										</a>
									{/if}
								</td>
								<td>
									{#if editing}
										<input type="number" step="any" bind:value={b.value} />
									{:else}
										{b.value}
									{/if}
								</td>
								<td>
									{#if editing}
										<input bind:value={b.unit} placeholder="g/dL" />
									{:else}
										{b.unit}
									{/if}
								</td>
								<td>
									{#if editing}
										<input type="number" step="any" bind:value={b.ref_range_low} placeholder="low" />
										<input type="number" step="any" bind:value={b.ref_range_high} placeholder="high" />
									{:else if b.ref_range_low != null || b.ref_range_high != null}
										{b.ref_range_low ?? '—'} – {b.ref_range_high ?? '—'}
									{:else}
										—
									{/if}
								</td>
								<td>
									{#if b.flag}<span class="flag flag-{b.flag}">{b.flag}</span>{/if}
								</td>
								{#if editing}
									<td>
										<button class="del" type="button" onclick={() => removeRow(i)}>×</button>
									</td>
								{/if}
							</tr>
						{/each}
					</tbody>
				</table>
				{#if editing}
					<button class="btn add" type="button" onclick={addRow}>+ Add biomarker</button>
				{/if}
			</div>

			{#if source.available}
				<div class="source">
					<div class="source-header">Source document</div>
					{#if source.mime?.startsWith('image/')}
						<img src={healthPanelSourceUrl(id)} alt="Lab report" />
					{:else}
						<embed src={healthPanelSourceUrl(id)} type={source.mime || 'application/pdf'} />
					{/if}
					<a class="source-link" href={healthPanelSourceUrl(id)} target="_blank" rel="noopener">
						Open in new tab
					</a>
				</div>
			{/if}
		</div>
	{/if}

	{#if confirmDelete}
		<div class="modal-backdrop" onclick={() => (confirmDelete = false)} role="presentation">
			<div class="modal" onclick={(e) => e.stopPropagation()} role="presentation">
				<h2>Delete this panel?</h2>
				<p>Removes the panel, all biomarkers, derived stat entries, and the source file.</p>
				<div class="modal-actions">
					<button class="btn" type="button" onclick={() => (confirmDelete = false)}>Cancel</button>
					<button class="btn danger" type="button" onclick={deletePanel}>Delete</button>
				</div>
			</div>
		</div>
	{/if}
</div>

<style>
	.page {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}
	.header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1rem;
	}
	.back {
		font-size: var(--text-xs);
		color: var(--text-muted);
		text-decoration: none;
	}
	h1 {
		font-size: var(--text-lg);
		font-weight: 500;
		margin: 0.25rem 0;
	}
	.lab, .type {
		color: var(--text-muted);
		font-weight: 400;
	}
	.header-meta { display: flex; flex-direction: column; gap: 0.4rem; min-width: 0; }
	.header-edit {
		display: grid;
		grid-template-columns: auto 1fr 1fr;
		gap: 0.5rem 0.75rem;
		max-width: 32rem;
	}
	.header-edit label {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		font-size: var(--text-sm);
		min-width: 0;
	}
	.header-edit label > span {
		color: var(--text-muted);
		font-size: var(--text-xs);
	}
	.header-edit input,
	.header-edit select {
		background: var(--surface-raised);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.3rem 0.5rem;
		min-width: 0;
	}
	.header-edit .full-row {
		grid-column: 1 / -1;
	}
	.encounter-link {
		font-size: var(--text-sm);
		color: var(--text-muted);
	}
	.encounter-label {
		color: var(--text-dim);
	}
	.encounter-link a {
		color: #7aa3d8;
		text-decoration: none;
	}
	.encounter-link a:hover {
		text-decoration: underline;
	}
	.badge {
		font-size: var(--text-xs);
		padding: 0.1rem 0.5rem;
		border-radius: var(--radius-pill);
	}
	.badge.draft { background: #3a3017; color: #e6b96b; }
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
	}
	.btn:hover:not(:disabled) { background: var(--surface-raised); }
	.btn:disabled { opacity: 0.6; cursor: not-allowed; }
	.btn.primary { border-color: #7aa3d8; color: #7aa3d8; }
	.btn.danger { border-color: #c66; color: #c66; }
	.split {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 1rem;
	}
	@media (max-width: 900px) {
		.split { grid-template-columns: 1fr; }
	}
	.biomarker-table table {
		width: 100%;
		border-collapse: collapse;
	}
	.biomarker-table th, .biomarker-table td {
		padding: 0.35rem 0.5rem;
		text-align: left;
		font-size: var(--text-sm);
		border-bottom: 1px solid var(--border-subtle);
	}
	.biomarker-table th {
		color: var(--text-dim);
		text-transform: uppercase;
		font-size: var(--text-xs);
		font-weight: 400;
	}
	.biomarker-table input {
		width: 100%;
		max-width: 9rem;
		background: var(--surface-raised);
		border: 1px solid var(--border-default);
		border-radius: 0.2rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.15rem 0.3rem;
	}
	.marker-link {
		color: var(--text-primary);
		text-decoration: none;
	}
	.marker-link:hover {
		text-decoration: underline;
	}
	.flag-row { background: rgba(204, 102, 102, 0.06); }
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
	.add { margin-top: 0.5rem; }
	.del {
		background: none; border: none; color: var(--text-dim);
		font-size: 1.1rem; cursor: pointer;
	}
	.del:hover { color: #c66; }
	.source {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.75rem;
	}
	.source-header {
		font-size: var(--text-sm);
		color: var(--text-muted);
	}
	.source img { width: 100%; height: auto; }
	.source embed { width: 100%; height: 600px; }
	.source-link {
		font-size: var(--text-xs);
		color: var(--text-muted);
	}
	.empty { color: var(--text-dim); padding: 2rem 0; }
	.msg {
		font-size: var(--text-sm);
		padding: 0.4rem 0.6rem;
		border-radius: 0.3rem;
	}
	.msg.error { background: rgba(204, 102, 102, 0.1); color: #f0a; }
	.msg.info { background: rgba(122, 163, 216, 0.1); color: #7aa3d8; }
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
	.modal h2 { font-size: var(--text-base); font-weight: 500; margin: 0 0 0.5rem; }
	.modal p { font-size: var(--text-sm); color: var(--text-muted); margin: 0 0 1rem; }
	.modal-actions { display: flex; justify-content: flex-end; gap: 0.5rem; }
</style>
