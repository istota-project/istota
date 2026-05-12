<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getHealthSettings,
		putHealthSettings,
		type HealthSettings,
	} from '$lib/api';

	let loading = $state(true);
	let saving = $state(false);
	let error = $state('');
	let info = $state('');

	let settings: HealthSettings = $state({
		dob: null,
		height_cm: null,
		sex: null,
		display_units: { weight: 'kg', height: 'cm', temp: 'C' },
	});

	let dobInput = $state('');
	let heightInput = $state('');

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await getHealthSettings();
			settings = resp.settings;
			dobInput = settings.dob || '';
			heightInput = settings.height_cm != null ? String(settings.height_cm) : '';
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	async function save() {
		saving = true;
		error = '';
		info = '';
		try {
			const payload: Partial<HealthSettings> = {
				dob: dobInput || null,
				height_cm: heightInput ? Number(heightInput) : null,
				sex: settings.sex || null,
				display_units: settings.display_units,
			};
			const resp = await putHealthSettings(payload);
			settings = resp.settings;
			info = 'Saved.';
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save';
		} finally {
			saving = false;
		}
	}

	const ageYears = $derived.by(() => {
		if (!dobInput) return null;
		try {
			const dob = new Date(dobInput);
			const diffMs = Date.now() - dob.getTime();
			const years = diffMs / (365.25 * 86400 * 1000);
			return Math.floor(years);
		} catch {
			return null;
		}
	});

	onMount(load);
</script>

<div class="page">
	<h1>Health settings</h1>

	{#if loading}
		<div class="empty">Loading…</div>
	{:else}
		<section class="card">
			<h2>Profile</h2>
			<div class="form">
				<label>
					<span>Date of birth</span>
					<input type="date" bind:value={dobInput} />
					{#if ageYears != null}
						<span class="age">Age: {ageYears}</span>
					{/if}
				</label>
				<label>
					<span>Height (cm)</span>
					<input type="number" step="0.1" bind:value={heightInput} placeholder="178" />
				</label>
				<label>
					<span>Biological sex</span>
					<select bind:value={settings.sex}>
						<option value={null}>—</option>
						<option value="M">Male</option>
						<option value="F">Female</option>
					</select>
					<span class="hint">Used for sex-specific reference ranges on biomarkers.</span>
				</label>
			</div>
		</section>

		<section class="card">
			<h2>Display preferences</h2>
			<p class="hint">All values are stored in metric. Choose how they're shown.</p>
			<div class="form">
				<label>
					<span>Weight</span>
					<select bind:value={settings.display_units.weight}>
						<option value="kg">kg</option>
						<option value="lb">lb</option>
					</select>
				</label>
				<label>
					<span>Height</span>
					<select bind:value={settings.display_units.height}>
						<option value="cm">cm</option>
						<option value="ft_in">ft / in</option>
					</select>
				</label>
				<label>
					<span>Temperature</span>
					<select bind:value={settings.display_units.temp}>
						<option value="C">°C</option>
						<option value="F">°F</option>
					</select>
				</label>
			</div>
		</section>

		{#if error}<div class="msg error">{error}</div>{/if}
		{#if info}<div class="msg info">{info}</div>{/if}

		<div class="actions">
			<button class="btn" onclick={save} disabled={saving} type="button">
				{saving ? 'Saving…' : 'Save'}
			</button>
		</div>
	{/if}
</div>

<style>
	.page {
		display: flex;
		flex-direction: column;
		gap: 1rem;
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
	}
	.form {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	label {
		display: grid;
		grid-template-columns: 9rem 12rem auto;
		gap: 0.75rem;
		align-items: center;
	}
	label > span:first-child {
		color: var(--text-muted);
		font-size: var(--text-sm);
	}
	input, select {
		background: var(--bg-input, var(--surface-raised));
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.3rem 0.5rem;
	}
	.hint, .age {
		color: var(--text-dim);
		font-size: var(--text-xs);
	}
	.actions {
		display: flex;
		gap: 0.5rem;
	}
	.btn {
		padding: 0.4rem 1rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		color: var(--text-primary);
		font: inherit;
		cursor: pointer;
	}
	.btn:hover:not(:disabled) {
		background: var(--surface-raised);
	}
	.btn:disabled { opacity: 0.6; cursor: not-allowed; }
	.msg {
		font-size: var(--text-sm);
		padding: 0.4rem 0.6rem;
		border-radius: 0.3rem;
	}
	.msg.error { background: rgba(204, 102, 102, 0.1); color: #f0a; }
	.msg.info { background: rgba(122, 163, 216, 0.1); color: #7aa3d8; }
	.empty { color: var(--text-dim); padding: 1rem 0; }
</style>
