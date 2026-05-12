<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import {
		extractHealthPanel,
		saveHealthBiomarkers,
		uploadHealthPanel,
		healthPanelSourceUrl,
		type Biomarker,
	} from '$lib/api';

	let file: File | null = $state(null);
	let drawnAt = $state(new Date().toISOString().slice(0, 10));
	let labName = $state('');
	let panelType = $state('');

	let uploading = $state(false);
	let extracting = $state(false);
	let saving = $state(false);

	let panelId: number | null = $state(null);
	let mime: string | null = $state(null);
	let collision: { existing_id: number; drawn_at: string; lab_name: string | null } | null =
		$state(null);
	let warnings: string[] = $state([]);
	let extracted: Partial<Biomarker>[] = $state([]);

	let error = $state('');
	let info = $state('');

	function handleFile(e: Event) {
		const input = e.target as HTMLInputElement;
		file = input.files?.[0] ?? null;
	}

	function handleDrop(e: DragEvent) {
		e.preventDefault();
		const f = e.dataTransfer?.files?.[0];
		if (f) file = f;
	}

	async function doUpload() {
		if (!file) {
			error = 'Please pick a file first.';
			return;
		}
		error = '';
		info = '';
		uploading = true;
		try {
			const resp = await uploadHealthPanel(file, drawnAt, labName || undefined, panelType || undefined);
			panelId = resp.id;
			mime = file.type || null;
			collision = resp.collision ?? null;
			info = 'Uploaded. Running extraction…';
			await doExtract();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Upload failed';
		} finally {
			uploading = false;
		}
	}

	async function doExtract() {
		if (panelId == null) return;
		extracting = true;
		try {
			const resp = await extractHealthPanel(panelId);
			extracted = resp.biomarkers.map((b, idx) => ({
				id: -(idx + 1),
				panel_id: panelId!,
				name: b.name || '',
				display_name: b.display_name ?? null,
				value: Number(b.value ?? 0),
				unit: b.unit || '',
				ref_range_low: b.ref_range_low ?? null,
				ref_range_high: b.ref_range_high ?? null,
				flag: b.flag ?? null,
			}));
			warnings = resp.warnings || [];
			info = `Extracted ${extracted.length} biomarkers. Review and confirm.`;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Extraction failed';
		} finally {
			extracting = false;
		}
	}

	function addRow() {
		extracted = [
			...extracted,
			{
				id: -Date.now(),
				panel_id: panelId!,
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

	function removeRow(i: number) {
		extracted = extracted.filter((_, idx) => idx !== i);
	}

	async function confirm() {
		if (panelId == null) return;
		saving = true;
		error = '';
		try {
			await saveHealthBiomarkers(
				panelId,
				extracted.map((b) => ({
					name: b.name!,
					display_name: b.display_name ?? undefined,
					value: Number(b.value),
					unit: b.unit!,
					ref_range_low: b.ref_range_low ?? undefined,
					ref_range_high: b.ref_range_high ?? undefined,
					flag: b.flag ?? undefined,
				})),
				true,
			);
			goto(`${base}/health/bloodwork/${panelId}`);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}
</script>

<div class="page">
	<a class="back" href="{base}/health/bloodwork">← Bloodwork</a>
	<h1>Upload lab results</h1>

	{#if panelId == null}
		<div class="card">
			<div
				class="dropzone"
				ondragover={(e) => e.preventDefault()}
				ondrop={handleDrop}
				role="presentation"
			>
				{#if file}
					<div class="picked">{file.name} <span class="hint">({Math.round(file.size / 1024)} KB)</span></div>
				{:else}
					<p>Drop a PDF or image of the lab report here, or use the file picker.</p>
				{/if}
				<input type="file" accept="image/*,application/pdf" onchange={handleFile} />
			</div>

			<div class="form">
				<label>
					<span>Date drawn</span>
					<input type="date" bind:value={drawnAt} required />
				</label>
				<label>
					<span>Lab</span>
					<input type="text" bind:value={labName} placeholder="Quest, Kaiser, …" />
				</label>
				<label>
					<span>Panel type</span>
					<input type="text" bind:value={panelType} placeholder="CBC, CMP, Lipid, …" />
				</label>
			</div>

			{#if error}<div class="msg error">{error}</div>{/if}
			{#if info}<div class="msg info">{info}</div>{/if}

			<div class="actions">
				<button class="btn primary" onclick={doUpload} disabled={uploading || !file} type="button">
					{uploading ? 'Uploading…' : 'Upload + extract'}
				</button>
			</div>
		</div>
	{:else}
		{#if collision}
			<div class="msg warn">
				A panel from {collision.lab_name || '—'} on {collision.drawn_at} already exists.
				This upload is saved separately;
				<a href="{base}/health/bloodwork/{collision.existing_id}">view the existing one</a>
				to decide which to keep.
			</div>
		{/if}

		<div class="split">
			<div class="review-table">
				<h2>Extracted biomarkers</h2>

				{#if extracting}
					<div class="empty extracting">
						<span class="spinner" aria-hidden="true"></span>
						Extracting biomarkers from the source file…
					</div>
				{:else}
					{#if warnings.length > 0}
						<div class="msg warn">
							<strong>Heads up:</strong>
							<ul>{#each warnings as w}<li>{w}</li>{/each}</ul>
						</div>
					{/if}

					{#if extracted.length === 0}
						<div class="empty">
							No biomarkers extracted yet. Add rows manually, or retry extraction.
						</div>
					{:else}
						<table>
							<thead>
								<tr>
									<th>Marker</th><th>Value</th><th>Unit</th><th>Range (low / high)</th><th>Flag</th><th></th>
								</tr>
							</thead>
							<tbody>
								{#each extracted as b, i (b.id)}
									<tr>
										<td><input bind:value={b.name} placeholder="Hemoglobin" /></td>
										<td><input type="number" step="any" bind:value={b.value} /></td>
										<td><input bind:value={b.unit} placeholder="g/dL" /></td>
										<td class="range-pair">
											<input type="number" step="any" bind:value={b.ref_range_low} placeholder="low" />
											<input type="number" step="any" bind:value={b.ref_range_high} placeholder="high" />
										</td>
										<td>
											<select bind:value={b.flag}>
												<option value={null}>—</option>
												<option value="H">H</option>
												<option value="L">L</option>
												<option value="C">C</option>
											</select>
										</td>
										<td><button class="del" type="button" onclick={() => removeRow(i)}>×</button></td>
									</tr>
								{/each}
							</tbody>
						</table>
					{/if}

					{#if error}<div class="msg error">{error}</div>{/if}

					<div class="actions">
						<button class="btn" onclick={addRow} type="button">+ Add row</button>
						<button class="btn" type="button" onclick={doExtract}>Retry extraction</button>
						<div class="spacer"></div>
						<button class="btn primary" disabled={saving || extracted.length === 0} onclick={confirm} type="button">
							{saving ? 'Saving…' : 'Confirm and save'}
						</button>
					</div>
				{/if}
			</div>

			<div class="source">
				<div class="source-header">Source preview</div>
				{#if mime?.startsWith('image/')}
					<img src={healthPanelSourceUrl(panelId)} alt="Lab report" />
				{:else}
					<embed src={healthPanelSourceUrl(panelId)} type={mime || 'application/pdf'} />
				{/if}
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
	.back {
		font-size: var(--text-xs);
		color: var(--text-muted);
		text-decoration: none;
	}
	h1 {
		font-size: var(--text-lg);
		font-weight: 500;
		margin: 0;
	}
	h2 {
		font-size: var(--text-base);
		font-weight: 500;
		margin: 0 0 0.5rem;
	}
	.card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	.dropzone {
		border: 2px dashed var(--border-default);
		border-radius: var(--radius-card);
		padding: 2rem;
		text-align: center;
		color: var(--text-muted);
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.picked { color: var(--text-primary); }
	.hint { color: var(--text-dim); font-size: var(--text-xs); }
	.form {
		display: grid;
		grid-template-columns: 1fr 1fr 1fr;
		gap: 0.5rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: var(--text-sm);
	}
	label > span { color: var(--text-muted); font-size: var(--text-xs); }
	input, select {
		background: var(--surface-raised);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.3rem 0.5rem;
	}
	.actions {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		margin-top: 0.75rem;
	}
	.actions .spacer { flex: 1; }
	.extracting {
		display: flex;
		align-items: center;
		gap: 0.5rem;
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
	.btn {
		padding: 0.4rem 0.85rem;
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
	.split {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 1rem;
	}
	@media (max-width: 900px) {
		.split { grid-template-columns: 1fr; }
	}
	.review-table table {
		width: 100%;
		border-collapse: collapse;
	}
	.review-table th, .review-table td {
		padding: 0.3rem 0.4rem;
		text-align: left;
		font-size: var(--text-sm);
		border-bottom: 1px solid var(--border-subtle);
	}
	.review-table th {
		color: var(--text-dim);
		text-transform: uppercase;
		font-size: var(--text-xs);
		font-weight: 400;
	}
	.range-pair { display: flex; gap: 0.25rem; }
	.range-pair input { max-width: 5rem; }
	.review-table input {
		max-width: 9rem;
		font-size: var(--text-xs);
		padding: 0.15rem 0.3rem;
	}
	.del { background: none; border: none; color: var(--text-dim); cursor: pointer; }
	.del:hover { color: #c66; }
	.empty { color: var(--text-dim); padding: 0.5rem 0; }
	.source {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.75rem;
	}
	.source-header { font-size: var(--text-sm); color: var(--text-muted); margin-bottom: 0.5rem; }
	.source img { width: 100%; height: auto; }
	.source embed { width: 100%; height: 600px; }
	.msg {
		font-size: var(--text-sm);
		padding: 0.5rem 0.75rem;
		border-radius: 0.3rem;
	}
	.msg.error { background: rgba(204, 102, 102, 0.1); color: #f0a; }
	.msg.info { background: rgba(122, 163, 216, 0.1); color: #7aa3d8; }
	.msg.warn { background: rgba(230, 185, 107, 0.1); color: #e6b96b; }
	.msg ul { margin: 0.25rem 0 0 1rem; padding: 0; }
</style>
