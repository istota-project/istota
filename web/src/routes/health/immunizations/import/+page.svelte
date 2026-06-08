<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import {
		bulkInsertImmunizations,
		extractImmunizations,
		listImmunizationRefs,
		parseImmunizations,
		type ImmunizationRef,
		type ParsedImmunization,
	} from '$lib/api';

	type Mode = 'file' | 'paste';
	let mode: Mode = $state('file');

	// File path
	let file: File | null = $state(null);
	let fileInput: HTMLInputElement | undefined = $state();
	let extracting = $state(false);
	let extractMode: 'text' | 'vision' | null = $state(null);

	// Paste path
	let raw = $state('');
	let parsing = $state(false);

	// Shared state
	let importing = $state(false);
	let error = $state('');
	let warnings: string[] = $state([]);
	let parsed: ParsedImmunization[] = $state([]);
	let refs: ImmunizationRef[] = $state([]);

	onMount(async () => {
		try {
			const r = await listImmunizationRefs();
			refs = r.refs;
		} catch {
			// non-fatal
		}
	});

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
		// Image paste only — the textarea handles plain-text paste itself.
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
			const out = await extractImmunizations(file);
			parsed = out.rows;
			warnings = out.warnings || [];
			extractMode = out.mode;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Extraction failed';
		} finally {
			extracting = false;
		}
	}

	async function doParse() {
		error = '';
		warnings = [];
		parsing = true;
		try {
			const out = await parseImmunizations(raw);
			parsed = out.rows;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to parse';
		} finally {
			parsing = false;
		}
	}

	async function doImport() {
		error = '';
		const missing = parsed.filter((r) => !r.date_given);
		if (missing.length > 0) {
			error = `${missing.length} row(s) need a date before import. Edit or remove them first.`;
			return;
		}
		importing = true;
		try {
			const out = await bulkInsertImmunizations(parsed);
			if (out.status === 'ok') {
				await goto(`${base}/health/immunizations`);
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

	function switchMode(m: Mode) {
		if (m === mode) return;
		mode = m;
		parsed = [];
		warnings = [];
		error = '';
		extractMode = null;
	}
</script>

<div class="header">
	<h1>Import immunizations</h1>
	<a class="btn" href="{base}/health/immunizations">Back</a>
</div>

<div class="tabs" role="tablist">
	<button
		type="button"
		role="tab"
		aria-selected={mode === 'file'}
		class="tab"
		class:active={mode === 'file'}
		onclick={() => switchMode('file')}
	>
		Screenshot or PDF
	</button>
	<button
		type="button"
		role="tab"
		aria-selected={mode === 'paste'}
		class="tab"
		class:active={mode === 'paste'}
		onclick={() => switchMode('paste')}
	>
		Paste text
	</button>
</div>

{#if mode === 'file'}
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
					Drop, paste, or pick a screenshot or PDF of your immunization list.
				</p>
				<p class="hint">
					The LLM extracts vaccine name + date and matches each row to a
					canonical family. You'll review the table before anything is saved.
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
{:else}
	<div class="card">
		<p class="hint">
			Paste a MyChart / EHR vaccine list below. Lines like
			<code>"Influenza (Given 11/28/2025)"</code> are recognised and matched
			to a canonical vaccine family. Review the table before importing.
		</p>
		<textarea
			class="paste"
			rows="10"
			bind:value={raw}
			placeholder={'INFS Pres Free 6mos-Adult (Fluzone trivalent) (influenza) (Given 11/28/2025)\nTdap (Tetanus, diphtheria, acellular pertussis) (Given 12/1/2016)\nTYDvi (Typhoid, ViCPs) (Given 10/23/2023)'}
		></textarea>
		<div class="actions">
			<button
				class="btn primary"
				type="button"
				disabled={!raw.trim() || parsing}
				onclick={doParse}
			>
				{parsing ? 'Parsing…' : 'Parse'}
			</button>
		</div>
	</div>
{/if}

{#if error}
	<div class="msg error">{error}</div>
{/if}

{#if extracting}
	<div class="card extracting">
		<span class="spinner" aria-hidden="true"></span>
		Extracting immunizations from the source — this can take a few seconds.
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
		<h2>Review {parsed.length} row{parsed.length === 1 ? '' : 's'}</h2>
		{#if extractMode}
			<span class="meta">Extracted via {extractMode === 'vision' ? 'vision' : 'text'} mode</span>
		{/if}
	</div>

	<div class="table-scroll">
		<table class="grid">
			<thead>
				<tr>
					<th>Vaccine</th>
					<th>Date</th>
					<th>Product</th>
					<th>Confidence</th>
					{#if mode === 'paste'}
						<th>Source line</th>
					{/if}
					<th class="row-actions"></th>
				</tr>
			</thead>
			<tbody>
				{#each parsed as row, i (i)}
					<tr class:warn={row.name === 'Unknown' || !row.date_given}>
						<td>
							<select bind:value={row.name}>
								<option value="Unknown">Unknown — leave as note</option>
								{#each refs as r (r.name)}
									<option value={r.name}>{r.display_name}</option>
								{/each}
							</select>
						</td>
						<td>
							<input type="date" bind:value={row.date_given} />
						</td>
						<td>
							<input
								type="text"
								value={row.product_name || ''}
								oninput={(e) =>
									(row.product_name =
										(e.currentTarget as HTMLInputElement).value || null)}
							/>
						</td>
						<td>
							<span class="badge conf-{row.confidence}">{row.confidence}</span>
						</td>
						{#if mode === 'paste'}
							<td class="src">{row.source_line}</td>
						{/if}
						<td class="row-actions">
							<button class="btn small" type="button" onclick={() => removeRow(i)}>
								Remove
							</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	<div class="actions">
		<button class="btn primary" type="button" disabled={importing} onclick={doImport}>
			{importing ? 'Importing…' : `Import ${parsed.length} rows`}
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

	.tabs {
		display: flex;
		gap: 0.25rem;
		margin-bottom: 0.75rem;
		border-bottom: 1px solid var(--border-subtle);
	}
	.tab {
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.45rem 0.75rem;
		cursor: pointer;
		margin-bottom: -1px;
	}
	.tab:hover { color: var(--text-primary); }
	.tab.active {
		color: var(--text-primary);
		border-bottom-color: #7aa3d8;
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

	.paste {
		width: 100%;
		padding: 0.5rem 0.65rem;
		background: var(--surface-raised);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		color: var(--text-primary);
		font-family: var(--font-mono, ui-monospace, SFMono-Regular, monospace);
		font-size: var(--text-sm);
		box-sizing: border-box;
		resize: vertical;
	}
	.paste:focus { outline: 1px solid #7aa3d8; }

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
	code {
		background: var(--surface-raised);
		padding: 0 0.3rem;
		border-radius: 0.2rem;
		font-size: 0.85em;
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
	tr.warn td {
		background: hsla(35, 60%, 60%, 0.08);
	}
	td.row-actions,
	th.row-actions {
		text-align: right;
		white-space: nowrap;
	}
	td.src {
		max-width: 260px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono, ui-monospace, SFMono-Regular, monospace);
		font-size: 0.78rem;
		color: var(--text-dim);
	}
	input,
	select {
		padding: 0.25rem 0.4rem;
		background: var(--surface-base);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
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

	/* Light theme overrides — dark rules above untouched. */
	:global(:root[data-theme='light']) .btn.primary { border-color: #2563b0; color: #2563b0; }
	:global(:root[data-theme='light']) .tab.active { border-bottom-color: #2563b0; }
	:global(:root[data-theme='light']) .paste:focus { outline-color: #2563b0; }
	:global(:root[data-theme='light']) .msg.error { color: #c0271d; }
	:global(:root[data-theme='light']) .msg.warn { color: #946a00; }
	:global(:root[data-theme='light']) .badge.conf-high { color: #15803d; }
	:global(:root[data-theme='light']) .badge.conf-medium { color: #946a00; }
	:global(:root[data-theme='light']) .badge.conf-low { color: #c0271d; }
</style>
