<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getHealthSettings,
		putHealthSettings,
		type HealthSettings,
	} from '$lib/api';
	import { Button, Select } from '$lib/components/ui';
	import {
		SettingsLayout,
		SettingsCard,
		SettingsField,
	} from '$lib/components/settings';
	import { cmToFtIn, ftInToCm } from '$lib/health/units';
	import GarminCard from './GarminCard.svelte';

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

	const sexOptions = [
		{ value: '', label: '—' },
		{ value: 'M', label: 'Male' },
		{ value: 'F', label: 'Female' },
	];
	const weightUnitOptions = [
		{ value: 'kg', label: 'kg' },
		{ value: 'lb', label: 'lb' },
	];
	const heightUnitOptions = [
		{ value: 'cm', label: 'cm' },
		{ value: 'ft_in', label: 'ft / in' },
	];
	const tempUnitOptions = [
		{ value: 'C', label: '°C' },
		{ value: 'F', label: '°F' },
	];

	let dobInput = $state('');
	let heightInput = $state('');
	let heightFtInput = $state('');
	let heightInInput = $state('');

	// Compute the effective height in cm based on the active input mode.
	function effectiveHeightCm(): number | null {
		if (settings.display_units.height === 'ft_in') {
			const ft = Number(heightFtInput);
			const inches = Number(heightInInput);
			if (!heightFtInput && !heightInInput) return null;
			if (!Number.isFinite(ft) || !Number.isFinite(inches)) return null;
			return Math.round(ftInToCm(ft, inches) * 10) / 10;
		}
		if (!heightInput) return null;
		const n = Number(heightInput);
		return Number.isFinite(n) ? n : null;
	}

	// Dirty tracking: compare a snapshot of the loaded form state to the
	// current values so the Save button only lights up when there's
	// something to save.
	let initialJson = $state('');
	let currentJson = $derived(
		JSON.stringify({
			dob: dobInput,
			height_cm:
				settings.display_units.height === 'ft_in'
					? `${heightFtInput}|${heightInInput}`
					: heightInput,
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
			syncHeightInputs(settings.height_cm);
			snapshot();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	function syncHeightInputs(cm: number | null): void {
		// Only populate the input matching the active display unit; leave
		// the other empty so a unit toggle reads cleanly from the
		// already-populated side.
		heightInput = '';
		heightFtInput = '';
		heightInInput = '';
		if (cm == null) return;
		if (settings.display_units.height === 'ft_in') {
			const { feet, inches } = cmToFtIn(cm);
			heightFtInput = String(feet);
			heightInInput = String(inches);
		} else {
			heightInput = String(Math.round(cm * 10) / 10);
		}
	}

	// When the user toggles the height display unit, populate the
	// newly-visible input from the other (without touching the off-screen
	// input, so toggling back is a no-op for the dirty check).
	let prevHeightUnit = $state<'cm' | 'ft_in' | null>(null);
	$effect(() => {
		const u = settings.display_units.height;
		if (prevHeightUnit !== null && u !== prevHeightUnit) {
			if (u === 'ft_in' && heightInput && !heightFtInput && !heightInInput) {
				const { feet, inches } = cmToFtIn(Number(heightInput));
				heightFtInput = String(feet);
				heightInInput = String(inches);
			} else if (u === 'cm' && (heightFtInput || heightInInput) && !heightInput) {
				const ft = Number(heightFtInput) || 0;
				const inches = Number(heightInInput) || 0;
				heightInput = String(Math.round(ftInToCm(ft, inches) * 10) / 10);
			}
		}
		prevHeightUnit = u;
	});

	async function save() {
		saving = true;
		error = '';
		info = '';
		try {
			const payload: Partial<HealthSettings> = {
				dob: dobInput || null,
				height_cm: effectiveHeightCm(),
				sex: settings.sex || null,
				display_units: settings.display_units,
			};
			const resp = await putHealthSettings(payload);
			settings = resp.settings;
			syncHeightInputs(settings.height_cm);
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

		<SettingsField label="Height">
			{#if settings.display_units.height === 'ft_in'}
				<div class="ft-in-row">
					<input type="number" step="1" min="0" bind:value={heightFtInput} placeholder="5" aria-label="Feet" />
					<span class="suffix">ft</span>
					<input type="number" step="0.1" min="0" bind:value={heightInInput} placeholder="10" aria-label="Inches" />
					<span class="suffix">in</span>
				</div>
			{:else}
				<div class="cm-row">
					<input type="number" step="0.1" bind:value={heightInput} placeholder="178" />
					<span class="suffix">cm</span>
				</div>
			{/if}
		</SettingsField>

		<SettingsField
			label="Biological sex"
			hint="Used for sex-specific reference ranges on biomarkers."
		>
			<Select
				value={settings.sex ?? ''}
				options={sexOptions}
				onValueChange={(v) => (settings.sex = v === '' ? null : (v as 'M' | 'F'))}
				ariaLabel="Biological sex"
				fullWidth
			/>
		</SettingsField>
	</SettingsCard>

	<SettingsCard
		title="Display preferences"
		description="All values are stored in metric. Choose how they're shown."
	>
		<SettingsField label="Weight">
			<Select
				value={settings.display_units.weight}
				options={weightUnitOptions}
				onValueChange={(v) => (settings.display_units.weight = v as 'kg' | 'lb')}
				ariaLabel="Weight unit"
				fullWidth
			/>
		</SettingsField>

		<SettingsField label="Height">
			<Select
				value={settings.display_units.height}
				options={heightUnitOptions}
				onValueChange={(v) => (settings.display_units.height = v as 'cm' | 'ft_in')}
				ariaLabel="Height unit"
				fullWidth
			/>
		</SettingsField>

		<SettingsField label="Temperature">
			<Select
				value={settings.display_units.temp}
				options={tempUnitOptions}
				onValueChange={(v) => (settings.display_units.temp = v as 'C' | 'F')}
				ariaLabel="Temperature unit"
				fullWidth
			/>
		</SettingsField>
	</SettingsCard>

	<GarminCard />
</SettingsLayout>

<style>
	.ft-in-row,
	.cm-row {
		display: flex;
		gap: 0.4rem;
		align-items: center;
	}
	.ft-in-row input,
	.cm-row input {
		flex: 1;
		min-width: 0;
	}
	.suffix {
		font-size: var(--text-sm);
		color: var(--text-muted);
	}
</style>
