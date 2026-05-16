<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import {
		deleteImmunization,
		getImmunization,
		updateImmunization,
		type Encounter,
		type Immunization,
	} from '$lib/api';

	let id = $derived(Number(page.url.searchParams.get('id')) || 0);
	let loading = $state(true);
	let saving = $state(false);
	let error = $state('');
	let formError = $state('');
	let immunization: Immunization | null = $state(null);
	let encounter: Encounter | null = $state(null);

	async function load() {
		if (!id) return;
		loading = true;
		error = '';
		try {
			const out = await getImmunization(id);
			immunization = out.immunization;
			encounter = out.encounter;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load';
		} finally {
			loading = false;
		}
	}

	async function save(e: Event) {
		e.preventDefault();
		if (!immunization) return;
		formError = '';
		saving = true;
		try {
			await updateImmunization(immunization.id, {
				name: immunization.name,
				date_given: immunization.date_given,
				product_name: immunization.product_name,
				manufacturer: immunization.manufacturer,
				dose_label: immunization.dose_label,
				lot_number: immunization.lot_number,
				route: immunization.route,
				site: immunization.site,
				administered_by: immunization.administered_by,
				facility: immunization.facility,
				cvx_code: immunization.cvx_code,
				notes: immunization.notes,
			});
			await load();
		} catch (e) {
			formError = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}

	async function remove() {
		if (!immunization || !confirm('Delete this immunization?')) return;
		try {
			await deleteImmunization(immunization.id);
			await goto(`${base}/health/immunizations`);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete';
		}
	}

	$effect(() => {
		if (id) load();
	});

	onMount(() => {
		if (id) load();
	});
</script>

<div class="header">
	<h1>Immunization detail</h1>
	<div class="actions">
		<a class="btn" href="{base}/health/immunizations">Back</a>
		{#if immunization}
			<a class="btn" href="{base}/health/immunizations/vaccine?name={encodeURIComponent(immunization.name)}">
				View all {immunization.name}
			</a>
			<button class="btn danger" type="button" onclick={remove}>Delete</button>
		{/if}
	</div>
</div>

{#if loading}
	<div class="empty">Loading…</div>
{:else if error}
	<div class="msg error">{error}</div>
{:else if immunization}
	<form class="form" onsubmit={save}>
		<div class="row">
			<label>
				<span>Vaccine name</span>
				<input type="text" bind:value={immunization.name} required />
			</label>
			<label>
				<span>Date given</span>
				<input type="date" bind:value={immunization.date_given} required />
			</label>
			<label>
				<span>Product</span>
				<input
					type="text"
					value={immunization.product_name ?? ''}
					oninput={(e) =>
						(immunization!.product_name = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>
			<label>
				<span>Manufacturer</span>
				<input
					type="text"
					value={immunization.manufacturer ?? ''}
					oninput={(e) =>
						(immunization!.manufacturer = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>
			<label>
				<span>Dose label</span>
				<input
					type="text"
					value={immunization.dose_label ?? ''}
					oninput={(e) =>
						(immunization!.dose_label = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>
			<label>
				<span>Lot number</span>
				<input
					type="text"
					value={immunization.lot_number ?? ''}
					oninput={(e) =>
						(immunization!.lot_number = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>
			<label>
				<span>Route</span>
				<select
					value={immunization.route ?? ''}
					onchange={(e) =>
						(immunization!.route = (e.currentTarget as HTMLSelectElement).value || null)}
				>
					<option value=""></option>
					<option value="IM">IM</option>
					<option value="SC">SC</option>
					<option value="oral">Oral</option>
					<option value="nasal">Nasal</option>
				</select>
			</label>
			<label>
				<span>Site</span>
				<input
					type="text"
					value={immunization.site ?? ''}
					oninput={(e) =>
						(immunization!.site = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>
			<label>
				<span>Administered by</span>
				<input
					type="text"
					value={immunization.administered_by ?? ''}
					oninput={(e) =>
						(immunization!.administered_by = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>
			<label>
				<span>Facility</span>
				<input
					type="text"
					value={immunization.facility ?? ''}
					oninput={(e) =>
						(immunization!.facility = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>
			<label>
				<span>CVX code</span>
				<input
					type="text"
					value={immunization.cvx_code ?? ''}
					oninput={(e) =>
						(immunization!.cvx_code = (e.currentTarget as HTMLInputElement).value || null)}
				/>
			</label>
		</div>
		<label class="full">
			<span>Notes</span>
			<textarea
				rows="3"
				value={immunization.notes ?? ''}
				oninput={(e) =>
					(immunization!.notes = (e.currentTarget as HTMLTextAreaElement).value || null)}
			></textarea>
		</label>
		<div class="meta muted">
			Source: {immunization.source}
			{#if immunization.created_at}
				· Created: {immunization.created_at}
			{/if}
		</div>
		{#if formError}
			<div class="msg error">{formError}</div>
		{/if}
		<div class="form-actions">
			<button class="btn primary" type="submit" disabled={saving}>
				{saving ? 'Saving…' : 'Save'}
			</button>
		</div>
	</form>

	{#if encounter}
		<section class="linked">
			<h2>Linked encounter</h2>
			<a class="card" href="{base}/health/history/encounter?id={encounter.id}">
				<div class="card-head">
					<span class="badge">{encounter.encounter_type}</span>
					<span>{encounter.encounter_date}</span>
				</div>
				{#if encounter.provider || encounter.facility}
					<div class="muted">
						{encounter.provider || ''}{encounter.provider && encounter.facility ? ' · ' : ''}{encounter.facility || ''}
					</div>
				{/if}
			</a>
		</section>
	{/if}
{:else}
	<div class="empty">Immunization not found.</div>
{/if}

<style>
	.header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		margin-bottom: 1rem;
	}
	.header h1 {
		font-size: 1.5rem;
		margin: 0;
	}
	.actions {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
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
	.btn.danger {
		color: var(--danger, #c0392b);
	}
	.form {
		border: 1px solid var(--border, #ddd);
		border-radius: 8px;
		padding: 1rem;
		background: var(--surface, #fff);
	}
	.row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: 0.75rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.85rem;
	}
	label.full {
		display: block;
		margin-top: 0.75rem;
	}
	label span {
		color: var(--muted, #666);
		font-size: 0.75rem;
	}
	input,
	select,
	textarea {
		padding: 0.4rem 0.5rem;
		border: 1px solid var(--border, #ddd);
		border-radius: 4px;
		background: var(--surface, #fff);
		color: inherit;
		font: inherit;
	}
	textarea {
		resize: vertical;
		min-height: 3em;
	}
	.form-actions {
		margin-top: 0.75rem;
		display: flex;
		justify-content: flex-end;
	}
	.meta {
		margin-top: 0.5rem;
		font-size: 0.8rem;
	}
	.muted {
		color: var(--muted, #666);
	}
	.msg.error {
		color: var(--danger, #c0392b);
		font-size: 0.85rem;
		margin: 0.5rem 0;
	}
	.empty {
		padding: 2rem;
		text-align: center;
		color: var(--muted, #666);
	}
	.linked {
		margin-top: 1.5rem;
	}
	.linked h2 {
		font-size: 1.05rem;
		margin: 0 0 0.5rem;
	}
	.card {
		display: block;
		padding: 0.75rem;
		border: 1px solid var(--border, #ddd);
		border-radius: 6px;
		background: var(--surface, #fff);
		color: inherit;
		text-decoration: none;
	}
	.card-head {
		display: flex;
		gap: 0.5rem;
		align-items: baseline;
		margin-bottom: 0.25rem;
	}
	.badge {
		display: inline-block;
		padding: 0.1rem 0.4rem;
		border-radius: 3px;
		font-size: 0.7rem;
		background: #eee;
		color: #555;
		font-weight: 600;
		text-transform: uppercase;
	}
</style>
