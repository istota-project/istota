<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getHealthSettings,
		putHealthSettings,
		type HealthSettings,
	} from '$lib/api';
	import { Button } from '$lib/components/ui';
	import {
		SettingsLayout,
		SettingsCard,
		SettingsField,
	} from '$lib/components/settings';

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

	// Dirty tracking: compare a snapshot of the loaded form state to the
	// current values so the Save button only lights up when there's
	// something to save.
	let initialJson = $state('');
	let currentJson = $derived(
		JSON.stringify({
			dob: dobInput,
			height: heightInput,
			sex: settings.sex,
			units: settings.display_units,
		}),
	);
	let dirty = $derived(initialJson !== '' && currentJson !== initialJson);

	function snapshot(): void {
		initialJson = currentJson;
	}

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await getHealthSettings();
			settings = resp.settings;
			dobInput = settings.dob || '';
			heightInput = settings.height_cm != null ? String(settings.height_cm) : '';
			snapshot();
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
			snapshot();
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

<SettingsLayout
	title="Health settings"
	description="Profile basics and display preferences. All values are stored in metric; display unit choices only affect what you see."
	{loading}
	{error}
	{info}
>
	{#snippet headerActions()}
		{#if dirty}
			<span class="dirty-badge">Unsaved changes</span>
		{/if}
		<Button variant="primary" onclick={save} disabled={!dirty || saving}>
			{saving ? 'Saving…' : 'Save changes'}
		</Button>
	{/snippet}

	<SettingsCard title="Profile">
		<SettingsField
			label="Date of birth"
			hint={ageYears != null ? `Age: ${ageYears}` : undefined}
		>
			<input type="date" bind:value={dobInput} />
		</SettingsField>

		<SettingsField label="Height (cm)">
			<input type="number" step="0.1" bind:value={heightInput} placeholder="178" />
		</SettingsField>

		<SettingsField
			label="Biological sex"
			hint="Used for sex-specific reference ranges on biomarkers."
		>
			<select bind:value={settings.sex}>
				<option value={null}>—</option>
				<option value="M">Male</option>
				<option value="F">Female</option>
			</select>
		</SettingsField>
	</SettingsCard>

	<SettingsCard
		title="Display preferences"
		description="All values are stored in metric. Choose how they're shown."
	>
		<SettingsField label="Weight">
			<select bind:value={settings.display_units.weight}>
				<option value="kg">kg</option>
				<option value="lb">lb</option>
			</select>
		</SettingsField>

		<SettingsField label="Height">
			<select bind:value={settings.display_units.height}>
				<option value="cm">cm</option>
				<option value="ft_in">ft / in</option>
			</select>
		</SettingsField>

		<SettingsField label="Temperature">
			<select bind:value={settings.display_units.temp}>
				<option value="C">°C</option>
				<option value="F">°F</option>
			</select>
		</SettingsField>
	</SettingsCard>
</SettingsLayout>
