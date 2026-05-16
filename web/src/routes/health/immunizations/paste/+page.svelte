<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import {
		bulkInsertImmunizations,
		listImmunizationRefs,
		parseImmunizations,
		type ImmunizationRef,
		type ParsedImmunization,
	} from '$lib/api';

	let raw = $state('');
	let parsing = $state(false);
	let importing = $state(false);
	let error = $state('');
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

	async function doParse() {
		error = '';
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
</script>

<div class="header">
	<h1>Import immunizations</h1>
	<a class="btn" href="{base}/health/immunizations">Back</a>
</div>

<p class="hint">
	Paste a MyChart / EHR vaccine list below. The parser recognises lines like
	<code>"Influenza (Given 11/28/2025)"</code> and resolves the vaccine family
	against canonical aliases. Review the table, fix anything tagged
	<em>Unknown</em>, then import.
</p>

<textarea
	class="paste"
	rows="10"
	bind:value={raw}
	placeholder={'INFS Pres Free 6mos-Adult (Fluzone trivalent) (influenza) (Given 11/28/2025)\nTdap (Tetanus, diphtheria, acellular pertussis) (Given 12/1/2016)\nTYDvi (Typhoid, ViCPs) (Given 10/23/2023)'}
></textarea>

<div class="actions">
	<button class="btn primary" type="button" disabled={!raw.trim() || parsing} onclick={doParse}>
		{parsing ? 'Parsing…' : 'Parse'}
	</button>
	{#if parsed.length > 0}
		<button class="btn primary" type="button" disabled={importing} onclick={doImport}>
			{importing ? 'Importing…' : `Import ${parsed.length} rows`}
		</button>
	{/if}
</div>

{#if error}
	<div class="msg error">{error}</div>
{/if}

{#if parsed.length > 0}
	<div class="table-scroll">
		<table class="grid">
			<thead>
				<tr>
					<th>Vaccine</th>
					<th>Date</th>
					<th>Product</th>
					<th>Confidence</th>
					<th>Source line</th>
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
									(row.product_name = (e.currentTarget as HTMLInputElement).value || null)}
							/>
						</td>
						<td>
							<span class="badge conf-{row.confidence}">{row.confidence}</span>
						</td>
						<td class="src">{row.source_line}</td>
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
	.hint {
		margin: 0 0 0.75rem;
		font-size: var(--text-sm);
		color: var(--text-muted);
		max-width: 70ch;
	}
	.hint code {
		background: var(--surface-raised);
		padding: 0 0.3rem;
		border-radius: 0.2rem;
		font-size: 0.85em;
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
	.actions {
		display: flex;
		gap: 0.5rem;
		margin: 0.75rem 0;
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

	.table-scroll {
		width: 100%;
		overflow-x: auto;
		margin-top: 1rem;
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
</style>
