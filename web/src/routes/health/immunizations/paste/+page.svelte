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
			error = `${missing.length} row(s) need a date before import. Edit them or remove them first.`;
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

<p class="muted">
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
	<table>
		<thead>
			<tr>
				<th>Vaccine</th>
				<th>Date</th>
				<th>Product</th>
				<th>Confidence</th>
				<th>Source line</th>
				<th></th>
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
						<span class="conf conf-{row.confidence}">{row.confidence}</span>
					</td>
					<td class="src">{row.source_line}</td>
					<td>
						<button class="btn small" type="button" onclick={() => removeRow(i)}>
							Remove
						</button>
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<style>
	.header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 0.75rem;
	}
	.header h1 {
		margin: 0;
		font-size: 1.5rem;
	}
	.btn {
		display: inline-flex;
		align-items: center;
		padding: 0.4rem 0.75rem;
		border: 1px solid var(--border, #ddd);
		border-radius: 6px;
		background: var(--surface, #fff);
		color: inherit;
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
	.muted {
		color: var(--muted, #666);
		font-size: 0.9rem;
	}
	code {
		font-size: 0.85em;
		background: var(--surface, #f4f4f4);
		padding: 0 0.25rem;
		border-radius: 3px;
	}
	.paste {
		width: 100%;
		font-family: var(--font-mono, monospace);
		font-size: 0.875rem;
		padding: 0.5rem;
		border: 1px solid var(--border, #ddd);
		border-radius: 6px;
		background: var(--surface, #fff);
		box-sizing: border-box;
	}
	.actions {
		display: flex;
		gap: 0.5rem;
		margin: 0.75rem 0;
	}
	.msg.error {
		color: var(--danger, #c0392b);
		margin: 0.5rem 0;
		font-size: 0.85rem;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.875rem;
		margin-top: 1rem;
	}
	th,
	td {
		text-align: left;
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--border, #eee);
	}
	tr.warn {
		background: rgba(240, 160, 32, 0.08);
	}
	.conf {
		display: inline-block;
		padding: 0.1rem 0.4rem;
		border-radius: 3px;
		font-size: 0.75rem;
		font-weight: 600;
	}
	.conf-high {
		background: #dff5e8;
		color: #186b3a;
	}
	.conf-medium {
		background: #fff1d6;
		color: #8a5a00;
	}
	.conf-low {
		background: #fde6e6;
		color: #a22;
	}
	.conf-manual {
		background: #eee;
		color: #555;
	}
	td.src {
		max-width: 280px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono, monospace);
		font-size: 0.8rem;
		color: var(--muted, #666);
	}
	input,
	select {
		padding: 0.25rem 0.4rem;
		border: 1px solid var(--border, #ddd);
		border-radius: 4px;
		background: var(--surface, #fff);
		color: inherit;
		font: inherit;
	}
</style>
