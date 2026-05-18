<script lang="ts">
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import {
		bulkInsertEncounters,
		extractEncounters,
		type ParsedDiagnosis,
		type ParsedEncounter,
	} from '$lib/api';

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
	];

	const DIAGNOSIS_STATUSES = ['active', 'chronic', 'resolved'] as const;

	let file: File | null = $state(null);
	let fileInput: HTMLInputElement | undefined = $state();
	let extracting = $state(false);
	let extractMode: 'text' | 'vision' | null = $state(null);
	let importing = $state(false);
	let error = $state('');
	let warnings: string[] = $state([]);
	let parsed: ParsedEncounter[] = $state([]);

	function pickFile(e: Event) {
		const input = e.target as HTMLInputElement;
		file = input.files?.[0] ?? null;
	}

	function handleDrop(e: DragEvent) {
		e.preventDefault();
		const f = e.dataTransfer?.files?.[0];
		if (f) file = f;
	}

	function handleFilePaste(e: ClipboardEvent) {
		const f = e.clipboardData?.files?.[0];
		if (f) {
			e.preventDefault();
			file = f;
		}
	}

	function clearFile() {
		file = null;
		if (fileInput) fileInput.value = '';
	}

	async function doExtract() {
		if (!file) {
			error = 'Pick a file first.';
			return;
		}
		error = '';
		warnings = [];
		parsed = [];
		extractMode = null;
		extracting = true;
		try {
			const out = await extractEncounters(file);
			parsed = out.rows;
			warnings = out.warnings || [];
			extractMode = out.mode;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Extraction failed';
		} finally {
			extracting = false;
		}
	}

	async function doImport() {
		error = '';
		const missing = parsed.filter((r) => !r.encounter_date);
		if (missing.length > 0) {
			error = `${missing.length} row(s) need a date before import. Edit or remove them first.`;
			return;
		}
		importing = true;
		try {
			const out = await bulkInsertEncounters(parsed);
			if (out.status === 'ok') {
				await goto(`${base}/health/history`);
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to import';
		} finally {
			importing = false;
		}
	}

	function removeRow(i: number) {
		parsed = parsed.filter((_, idx) => idx !== i);
	}

	function addDiagnosis(row: ParsedEncounter) {
		row.diagnoses = [
			...row.diagnoses,
			{ name: '', icd10: null, status: 'active', severity: null },
		];
	}

	function removeDiagnosis(row: ParsedEncounter, j: number) {
		row.diagnoses = row.diagnoses.filter((_, idx) => idx !== j);
	}

	function setDiagnosisField<K extends keyof ParsedDiagnosis>(
		row: ParsedEncounter,
		j: number,
		key: K,
		value: ParsedDiagnosis[K],
	) {
		row.diagnoses[j][key] = value;
		row.diagnoses = row.diagnoses;
	}
</script>

<div class="header">
	<h1>Import encounter</h1>
	<a class="btn" href="{base}/health/history">Back</a>
</div>

<div class="card">
	<div
		class="dropzone"
		ondragover={(e) => e.preventDefault()}
		ondrop={handleDrop}
		onpaste={handleFilePaste}
		role="presentation"
	>
		{#if file}
			<div class="picked">
				{file.name}
				<span class="hint">({Math.round(file.size / 1024)} KB)</span>
				<button
					type="button"
					class="clear"
					onclick={clearFile}
					aria-label="Clear selected file"
				>×</button>
			</div>
		{:else}
			<p>
				Drop, paste, or pick a screenshot or PDF of your visit paperwork —
				after-visit summary, discharge note, referral letter, etc.
			</p>
			<p class="hint">
				The LLM extracts date, provider, facility, reason, and any
				diagnoses listed, then matches each to the canonical encounter
				type. You'll review everything before it's saved.
			</p>
		{/if}
		<input
			bind:this={fileInput}
			type="file"
			accept="image/*,application/pdf"
			onchange={pickFile}
			class:hidden={file !== null}
		/>
	</div>

	<div class="actions">
		<button
			class="btn primary"
			type="button"
			disabled={!file || extracting}
			onclick={doExtract}
		>
			{extracting ? 'Extracting…' : 'Extract'}
		</button>
	</div>
</div>

{#if error}
	<div class="msg error">{error}</div>
{/if}

{#if extracting}
	<div class="card extracting">
		<span class="spinner" aria-hidden="true"></span>
		Extracting encounter from the source — this can take a few seconds.
	</div>
{/if}

{#if warnings.length > 0}
	<div class="msg warn">
		<ul>
			{#each warnings as w (w)}
				<li>{w}</li>
			{/each}
		</ul>
	</div>
{/if}

{#if parsed.length > 0}
	<div class="review-head">
		<h2>Review {parsed.length} encounter{parsed.length === 1 ? '' : 's'}</h2>
		{#if extractMode}
			<span class="meta">Extracted via {extractMode === 'vision' ? 'vision' : 'text'} mode</span>
		{/if}
	</div>

	{#each parsed as row, i (i)}
		<div class="enc-card" class:warn={!row.encounter_date}>
			<div class="enc-head">
				<span class="badge conf-{row.confidence}">{row.confidence}</span>
				<button class="btn small" type="button" onclick={() => removeRow(i)}>
					Remove encounter
				</button>
			</div>

			<div class="grid">
				<label>
					<span>Date</span>
					<input type="date" bind:value={row.encounter_date} />
				</label>
				<label>
					<span>Type</span>
					<select bind:value={row.encounter_type}>
						{#each ENCOUNTER_TYPES as t (t)}
							<option value={t}>{t}</option>
						{/each}
					</select>
				</label>
				<label>
					<span>Provider</span>
					<input
						type="text"
						value={row.provider || ''}
						oninput={(e) =>
							(row.provider = (e.currentTarget as HTMLInputElement).value || null)}
					/>
				</label>
				<label>
					<span>Facility</span>
					<input
						type="text"
						value={row.facility || ''}
						oninput={(e) =>
							(row.facility = (e.currentTarget as HTMLInputElement).value || null)}
					/>
				</label>
				<label>
					<span>Specialty</span>
					<input
						type="text"
						value={row.specialty || ''}
						oninput={(e) =>
							(row.specialty = (e.currentTarget as HTMLInputElement).value || null)}
					/>
				</label>
			</div>

			<label class="full">
				<span>Reason</span>
				<input
					type="text"
					value={row.reason || ''}
					oninput={(e) =>
						(row.reason = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>

			<label class="full">
				<span>Notes</span>
				<textarea
					rows="3"
					value={row.notes || ''}
					oninput={(e) =>
						(row.notes = (e.currentTarget as HTMLTextAreaElement).value || null)}
				></textarea>
			</label>

			<div class="diag-head">
				<h3>Diagnoses ({row.diagnoses.length})</h3>
				<button class="btn small" type="button" onclick={() => addDiagnosis(row)}>
					+ Add diagnosis
				</button>
			</div>

			{#if row.diagnoses.length > 0}
				<div class="table-scroll">
					<table class="grid-tbl">
						<thead>
							<tr>
								<th>Name</th>
								<th>ICD-10</th>
								<th>Status</th>
								<th>Severity</th>
								<th class="row-actions"></th>
							</tr>
						</thead>
						<tbody>
							{#each row.diagnoses as d, j (j)}
								<tr>
									<td>
										<input
											type="text"
											value={d.name}
											oninput={(e) =>
												setDiagnosisField(
													row,
													j,
													'name',
													(e.currentTarget as HTMLInputElement).value,
												)}
										/>
									</td>
									<td>
										<input
											type="text"
											value={d.icd10 || ''}
											oninput={(e) =>
												setDiagnosisField(
													row,
													j,
													'icd10',
													(e.currentTarget as HTMLInputElement).value || null,
												)}
										/>
									</td>
									<td>
										<select
											value={d.status}
											onchange={(e) =>
												setDiagnosisField(
													row,
													j,
													'status',
													(e.currentTarget as HTMLSelectElement).value as ParsedDiagnosis['status'],
												)}
										>
											{#each DIAGNOSIS_STATUSES as s (s)}
												<option value={s}>{s}</option>
											{/each}
										</select>
									</td>
									<td>
										<select
											value={d.severity || ''}
											onchange={(e) => {
												const v = (e.currentTarget as HTMLSelectElement).value;
												setDiagnosisField(
													row,
													j,
													'severity',
													(v || null) as ParsedDiagnosis['severity'],
												);
											}}
										>
											<option value="">—</option>
											<option value="mild">mild</option>
											<option value="moderate">moderate</option>
											<option value="severe">severe</option>
										</select>
									</td>
									<td class="row-actions">
										<button
											class="btn small"
											type="button"
											onclick={() => removeDiagnosis(row, j)}
										>
											Remove
										</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>
	{/each}

	<div class="actions">
		<button class="btn primary" type="button" disabled={importing} onclick={doImport}>
			{importing
				? 'Importing…'
				: `Import ${parsed.length} encounter${parsed.length === 1 ? '' : 's'}`}
		</button>
	</div>
{/if}

<style>
	.header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.75rem;
	}
	h1 {
		font-size: var(--text-lg, 1.05rem);
		font-weight: 500;
		margin: 0;
	}
	h2 {
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
		font-weight: 500;
		margin: 0;
	}
	h3 {
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
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
	.btn:disabled { opacity: 0.6; cursor: not-allowed; }
	.btn:hover:not(:disabled) { background: var(--surface-raised); }
	.btn.primary { border-color: #7aa3d8; color: #7aa3d8; }
	.btn.small {
		padding: 0.2rem 0.55rem;
		font-size: var(--text-xs);
	}

	.card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.85rem 1rem;
		margin-bottom: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.dropzone {
		border: 2px dashed var(--border-default);
		border-radius: var(--radius-card);
		padding: 1.5rem;
		text-align: center;
		color: var(--text-muted);
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.picked {
		color: var(--text-primary);
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		justify-content: center;
	}
	.hint {
		color: var(--text-dim);
		font-size: var(--text-xs);
		margin: 0;
	}
	.clear {
		background: none;
		border: 1px solid var(--border-default);
		border-radius: 50%;
		color: var(--text-muted);
		width: 1.4rem;
		height: 1.4rem;
		line-height: 1;
		cursor: pointer;
		font-size: var(--text-sm);
	}
	.clear:hover { background: var(--surface-raised); color: var(--text-primary); }
	.hidden { display: none; }

	.actions {
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}
	.card .actions { margin-top: 0.25rem; }

	.extracting {
		flex-direction: row;
		align-items: center;
		gap: 0.5rem;
		color: var(--text-muted);
		font-size: var(--text-sm);
	}
	.spinner {
		display: inline-block;
		width: 0.85rem;
		height: 0.85rem;
		border: 2px solid var(--border-default);
		border-top-color: var(--text-muted);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin { to { transform: rotate(360deg); } }

	.msg {
		font-size: var(--text-sm);
		padding: 0.5rem 0.75rem;
		border-radius: 0.3rem;
		margin-bottom: 0.75rem;
	}
	.msg.error { background: rgba(204, 102, 102, 0.1); color: #e88; }
	.msg.warn { background: rgba(230, 185, 107, 0.1); color: #e6b96b; }
	.msg ul { margin: 0; padding-left: 1.1rem; }

	.review-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
		margin: 0.75rem 0 0.5rem;
	}
	.meta {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.enc-card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.85rem 1rem;
		margin-bottom: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	.enc-card.warn { background: hsla(35, 60%, 60%, 0.08); }
	.enc-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.65rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--text-sm);
		min-width: 0;
	}
	label > span {
		color: var(--text-muted);
		font-size: var(--text-xs);
	}
	input, select, textarea {
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
	textarea {
		resize: vertical;
		font-family: inherit;
	}

	.diag-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-top: 0.25rem;
		border-top: 1px solid var(--border-subtle);
		padding-top: 0.65rem;
	}

	.table-scroll {
		width: 100%;
		overflow-x: auto;
	}
	table.grid-tbl {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--text-sm);
	}
	table.grid-tbl th,
	table.grid-tbl td {
		text-align: left;
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--border-subtle);
		vertical-align: middle;
	}
	table.grid-tbl th {
		color: var(--text-dim);
		font-weight: 500;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	/* Compact inputs inside the diagnoses table — mirrors the immunization
	   review-table sizing so the nested grid doesn't overpower the card. */
	table.grid-tbl input,
	table.grid-tbl select {
		padding: 0.25rem 0.4rem;
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
	.badge.conf-high {
		background: hsla(145, 40%, 55%, 0.22);
		color: #9bd6a6;
	}
	.badge.conf-medium {
		background: hsla(35, 60%, 60%, 0.22);
		color: #e6b96b;
	}
	.badge.conf-low {
		background: hsla(0, 60%, 55%, 0.28);
		color: #ff9d96;
	}
	.badge.conf-manual {
		background: hsla(220, 8%, 60%, 0.18);
		color: var(--text-muted);
	}
</style>
