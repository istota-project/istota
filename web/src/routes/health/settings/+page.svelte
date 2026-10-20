<script lang="ts">
  import { onMount } from 'svelte';
  import { getHealthSettings, putHealthSettings, type HealthSettings } from '$lib/api';
  import { Select, Card, Field, Input } from '$lib/components/ui';
  import { SettingsLayout } from '$lib/components/settings';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { cmToFtIn, ftInToCm } from '$lib/health/units';
  import { trackChanges } from '$lib/ui/draft.svelte';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  // Until the settings have loaded once the form would show defaults, and a
  // save would write them over the stored values: a failure takes the pane.
  let loaded = $state(false);
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

  // What Save would change, compared on the visible height input only: the
  // other one is filled in on a unit toggle, and toggling back is no edit.
  const changes = trackChanges(() => ({
    dob: dobInput,
    height_cm:
      settings.display_units.height === 'ft_in' ? `${heightFtInput}|${heightInInput}` : heightInput,
    sex: settings.sex,
    units: settings.display_units,
  }));

  async function load() {
    loading = true;
    error = '';
    try {
      const resp = await getHealthSettings();
      settings = resp.settings;
      dobInput = settings.dob || '';
      syncHeightInputs(settings.height_cm);
      changes.mark();
      loaded = true;
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
      changes.mark();
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

  useSettingsSave(() => ({ dirty: changes.dirty, saving, save }));
</script>

<SettingsLayout
  title="Health settings"
  description="Profile basics and display preferences. All values are stored in metric; display unit choices only affect what you see."
  {loading}
  error={loaded ? error : ''}
  loadError={loaded ? '' : error}
  onRetry={load}
  {info}
>
  <Card title="Profile">
    <Field label="Date of birth" hint={ageYears != null ? `Age: ${ageYears}` : undefined}>
      <Input type="date" bind:value={dobInput} />
    </Field>

    <Field label="Height">
      {#if settings.display_units.height === 'ft_in'}
        <div class="hstack">
          <Input
            type="number"
            step="1"
            min="0"
            bind:value={heightFtInput}
            placeholder="5"
            suffix="ft"
            aria-label="Height in feet"
          />
          <Input
            type="number"
            step="0.1"
            min="0"
            bind:value={heightInInput}
            placeholder="10"
            suffix="in"
            aria-label="Height in inches"
          />
        </div>
      {:else}
        <Input type="number" step="0.1" bind:value={heightInput} placeholder="178" suffix="cm" />
      {/if}
    </Field>

    <Field label="Biological sex" hint="Used for sex-specific reference ranges on biomarkers.">
      <Select
        value={settings.sex ?? ''}
        options={sexOptions}
        onValueChange={(v) => (settings.sex = v === '' ? null : (v as 'M' | 'F'))}
        ariaLabel="Biological sex"
        fullWidth
      />
    </Field>
  </Card>

  <Card
    title="Display preferences"
    description="All values are stored in metric. Choose how they're shown."
  >
    <Field label="Weight">
      <Select
        value={settings.display_units.weight}
        options={weightUnitOptions}
        onValueChange={(v) => (settings.display_units.weight = v as 'kg' | 'lb')}
        ariaLabel="Weight unit"
        fullWidth
      />
    </Field>

    <Field label="Height">
      <Select
        value={settings.display_units.height}
        options={heightUnitOptions}
        onValueChange={(v) => (settings.display_units.height = v as 'cm' | 'ft_in')}
        ariaLabel="Height unit"
        fullWidth
      />
    </Field>

    <Field label="Temperature">
      <Select
        value={settings.display_units.temp}
        options={tempUnitOptions}
        onValueChange={(v) => (settings.display_units.temp = v as 'C' | 'F')}
        ariaLabel="Temperature unit"
        fullWidth
      />
    </Field>
  </Card>
</SettingsLayout>
