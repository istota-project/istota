<script lang="ts">
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import {
    deleteMedication,
    getMedication,
    updateMedication,
    type Encounter,
    type HealthDocument,
    type Medication,
    type MedicationCategory,
    type MedicationFields,
  } from '$lib/api';
  import {
    Alert,
    AutocompleteInput,
    Badge,
    Button,
    Card,
    Checkbox,
    ConfirmDialog,
    EmptyState,
    Field,
    FormGrid,
    FormStack,
    Input,
    LoadState,
    PageHeader,
    Select,
    TextArea,
    type SelectOption,
  } from '@minot/ui';
  import { createDraft } from '@minot/ui/ui/draft.svelte';
  import { formatDate } from '@minot/ui/dateFormat';
  import { notifyError } from '@minot/ui/stores/notices';
  import DocumentList from '$lib/components/health/DocumentList.svelte';

  /** The record as the form holds it: every text field a string, '' for none. */
  interface MedicationForm {
    name: string;
    generic_name: string;
    dose: string;
    dose_unit: string;
    frequency: string;
    route: string;
    category: MedicationCategory;
    prescriber: string;
    pharmacy: string;
    reason: string;
    start_date: string;
    end_date: string;
    active: boolean;
    notes: string;
  }

  const categoryOptions: SelectOption[] = [
    { value: 'prescription', label: 'Prescription' },
    { value: 'otc', label: 'OTC' },
    { value: 'supplement', label: 'Supplement' },
  ];
  // The list page's vocabulary; suggestions only.
  const frequencyOptions = [
    'daily',
    'twice_daily',
    'three_times_daily',
    'every_other_day',
    'weekly',
    'monthly',
    'as_needed',
  ];
  const routeChoices: SelectOption[] = [
    { value: '', label: '' },
    { value: 'oral', label: 'Oral' },
    { value: 'topical', label: 'Topical' },
    { value: 'injection', label: 'Injection' },
    { value: 'inhalation', label: 'Inhalation' },
    { value: 'sublingual', label: 'Sublingual' },
    { value: 'transdermal', label: 'Transdermal' },
    { value: 'nasal', label: 'Nasal' },
    { value: 'ophthalmic', label: 'Ophthalmic' },
    { value: 'other', label: 'Other' },
  ];

  function toForm(m: Medication): MedicationForm {
    return {
      name: m.name,
      generic_name: m.generic_name ?? '',
      dose: m.dose ?? '',
      dose_unit: m.dose_unit ?? '',
      frequency: m.frequency ?? '',
      route: m.route ?? '',
      category: m.category,
      prescriber: m.prescriber ?? '',
      pharmacy: m.pharmacy ?? '',
      reason: m.reason ?? '',
      start_date: m.start_date ?? '',
      end_date: m.end_date ?? '',
      active: m.active,
      notes: m.notes ?? '',
    };
  }

  /** The changed fields as the route takes them: an emptied field is `null`, which clears it. */
  function toPatch(changed: Partial<MedicationForm>): MedicationFields {
    const patch: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(changed)) {
      if (typeof value === 'string') {
        const trimmed = value.trim();
        patch[key] = key === 'name' ? trimmed : trimmed || null;
      } else {
        patch[key] = value;
      }
    }
    return patch as MedicationFields;
  }

  let id = $derived(Number(page.url.searchParams.get('id')) || 0);
  let loading = $state(true);
  let error = $state('');
  let medication: Medication | null = $state(null);
  let encounter: Encounter | null = $state(null);
  let documents: HealthDocument[] = $state([]);

  const draft = createDraft<MedicationForm | null>(null);
  let saving = $state(false);
  let formError = $state('');
  let endDateError = $state('');
  let nameError = $state('');
  let confirmDelete = $state(false);

  // A route whose stored value is not one of the choices is kept selectable,
  // so opening the form never rewrites it to the first option.
  const routeOptions = $derived.by(() => {
    const current = draft.value?.route ?? '';
    if (!current || routeChoices.some((o) => o.value === current)) return routeChoices;
    return [...routeChoices, { value: current, label: current }];
  });

  // Prescriber and pharmacy mean nothing for a supplement, as on the add form,
  // but a value already stored stays visible and editable.
  const showPrescribing = $derived(
    !!draft.value &&
      (draft.value.category !== 'supplement' || !!draft.value.prescriber || !!draft.value.pharmacy),
  );

  async function load() {
    if (!id) {
      loading = false;
      return;
    }
    loading = true;
    error = '';
    try {
      const out = await getMedication(id);
      medication = out.medication;
      encounter = out.encounter;
      documents = out.documents ?? [];
      draft.reset(toForm(out.medication));
      endDateError = '';
      nameError = '';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load medication';
    } finally {
      loading = false;
    }
  }

  function setActive(checked: boolean) {
    const form = draft.value;
    if (!form) return;
    // Taking it again undoes the stop, and the stop date with it; a planned end
    // can be entered again afterwards.
    if (checked) {
      form.end_date = '';
      endDateError = '';
    }
  }

  async function save() {
    const form = draft.value;
    if (!medication || !form) return;
    formError = '';
    nameError = form.name.trim() ? '' : 'Enter a name.';
    // Stopping here is a stop with a date, the same as Stop on the list. A row
    // stored stopped with no date is left alone; only turning Active off asks.
    const stopping = medication.active && !form.active;
    endDateError = stopping && !form.end_date ? 'Enter the date you stopped taking it.' : '';
    if (nameError || endDateError) return;
    const patch = toPatch(draft.changed());
    if (Object.keys(patch).length === 0) return;
    saving = true;
    try {
      await updateMedication(medication.id, patch);
      await load();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  async function remove() {
    if (!medication) return;
    try {
      await deleteMedication(medication.id);
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to delete medication');
      return;
    }
    await goto(`${base}/health/medications`);
  }

  $effect(() => {
    if (id) load();
    else loading = false;
  });
</script>

{#snippet headerActions()}
  <Button variant="danger" onclick={() => (confirmDelete = true)}>Delete</Button>
{/snippet}

<LoadState {loading} {error} empty={!medication} onRetry={load}>
  {#snippet header()}
    <PageHeader
      title={medication?.name ?? 'Medication'}
      back={{ href: `${base}/health/medications`, label: 'Medications' }}
      align="start"
      actions={medication ? headerActions : undefined}
    />
  {/snippet}
  {#snippet emptyState()}
    <EmptyState>Medication not found.</EmptyState>
  {/snippet}
  {#if medication && draft.value}
    <Card as="div">
      <FormStack as="form" onsubmit={save} disabled={saving} novalidate>
        <FormGrid>
          <Field label="Name" required error={nameError}>
            <Input type="text" bind:value={draft.value.name} invalid={!!nameError} />
          </Field>
          <Field label="Generic name">
            <Input type="text" bind:value={draft.value.generic_name} />
          </Field>
          <Field label="Category">
            <Select
              value={draft.value.category}
              options={categoryOptions}
              onValueChange={(v) => {
                if (draft.value) draft.value.category = v as MedicationCategory;
              }}
              ariaLabel="Category"
              fullWidth
            />
          </Field>
          <Field label="Dose">
            <Input type="text" bind:value={draft.value.dose} />
          </Field>
          <Field label="Unit">
            <Input type="text" bind:value={draft.value.dose_unit} />
          </Field>
          <Field label="Frequency">
            <AutocompleteInput bind:value={draft.value.frequency} options={frequencyOptions} />
          </Field>
          <Field label="Route">
            <Select
              value={draft.value.route}
              options={routeOptions}
              onValueChange={(v) => {
                if (draft.value) draft.value.route = v;
              }}
              ariaLabel="Route"
              fullWidth
            />
          </Field>
          {#if showPrescribing}
            <Field label="Prescriber">
              <Input type="text" bind:value={draft.value.prescriber} />
            </Field>
            <Field label="Pharmacy">
              <Input type="text" bind:value={draft.value.pharmacy} />
            </Field>
          {/if}
          <Field label="Reason">
            <Input type="text" bind:value={draft.value.reason} />
          </Field>
          <Field label="Start date">
            <Input type="date" bind:value={draft.value.start_date} />
          </Field>
          <Field label="End date" error={endDateError}>
            <Input
              type="date"
              bind:value={draft.value.end_date}
              invalid={!!endDateError}
              oninput={() => (endDateError = '')}
            />
          </Field>
        </FormGrid>
        <Checkbox label="Currently taking" bind:checked={draft.value.active} onchange={setActive} />
        <Field label="Notes">
          <TextArea rows={3} bind:value={draft.value.notes} />
        </Field>
        <p class="caption source">
          {medication.source === 'agent' ? 'Added in chat' : 'Added on the web'}
        </p>
        {#if formError}
          <Alert appearance="inline" variant="danger" role="alert">{formError}</Alert>
        {/if}
        <div class="form-actions">
          <Button variant="primary" type="submit" loading={saving} disabled={!draft.dirty}
            >Save</Button
          >
        </div>
      </FormStack>
    </Card>

    {#if encounter}
      <section class="linked">
        <h2 class="micro-label">Prescribed at</h2>
        <Card href="{base}/health/history/encounter?id={encounter.id}">
          <div class="card-head">
            <Badge>{encounter.encounter_type}</Badge>
            <span class="caption">{formatDate(encounter.encounter_date)}</span>
          </div>
          {#if encounter.provider || encounter.facility}
            <div class="caption">
              {[encounter.provider, encounter.facility].filter(Boolean).join(' · ')}
            </div>
          {/if}
        </Card>
      </section>
    {/if}

    <section class="linked">
      <h2 class="micro-label">Documents</h2>
      <DocumentList entityType="medication" entityId={medication.id} {documents} />
    </section>
  {/if}
</LoadState>

<ConfirmDialog
  bind:open={confirmDelete}
  title="Delete medication"
  message="Are you sure you want to delete this medication? This cannot be undone. To record that you stopped taking it, turn off Currently taking instead."
  confirmLabel="Delete"
  confirmVariant="danger"
  onConfirm={remove}
/>

<style>
  .source {
    margin: 0;
  }
  .linked {
    margin-top: var(--space-6);
  }
  .linked h2 {
    margin: 0 0 var(--space-2);
  }
  .linked .card-head {
    align-items: center;
  }
</style>
