<script lang="ts">
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    createMedication,
    deleteMedication,
    discontinueMedication,
    listMedications,
    type Medication,
    type MedicationCategory,
  } from '$lib/api';
  import {
    Alert,
    AutocompleteInput,
    Badge,
    Button,
    Card,
    ConfirmDialog,
    Disclosure,
    EmptyState,
    Field,
    FormGrid,
    FormModal,
    FormStack,
    Input,
    KebabMenu,
    LoadState,
    PageHeader,
    Select,
    TextArea,
    Timestamp,
    type KebabItem,
    type SelectOption,
  } from '@minot/ui';
  import { notifyError } from '@minot/ui/stores/notices';
  import { localDateISO } from '@minot/ui/dateFormat';

  let loading = $state(true);
  let error = $state('');
  let medications: Medication[] = $state([]);

  let formOpen = $state(false);
  let formName = $state('');
  let formCategory: MedicationCategory = $state('prescription');
  let formDose = $state('');
  let formUnit = $state('');
  let formFrequency = $state('');
  let formStart = $state(localDateISO());
  let formGeneric = $state('');
  let formRoute = $state('');
  let formPrescriber = $state('');
  let formPharmacy = $state('');
  let formReason = $state('');
  let formNotes = $state('');
  let saving = $state(false);
  let formError = $state('');

  const categoryOptions: SelectOption[] = [
    { value: 'prescription', label: 'Prescription' },
    { value: 'otc', label: 'OTC' },
    { value: 'supplement', label: 'Supplement' },
  ];
  const categoryBadge: Record<MedicationCategory, string> = {
    prescription: 'Rx',
    otc: 'OTC',
    supplement: 'Supplement',
  };
  // The skill's vocabulary. Suggestions only: free text is stored as typed.
  const frequencyOptions = [
    'daily',
    'twice_daily',
    'three_times_daily',
    'every_other_day',
    'weekly',
    'monthly',
    'as_needed',
  ];
  const routeOptions: SelectOption[] = [
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

  const active = $derived(medications.filter((m) => m.active));
  const prescriptions = $derived(active.filter((m) => m.category === 'prescription'));
  const otherActive = $derived(active.filter((m) => m.category !== 'prescription'));
  // Newest stop first; a stop with no known date goes last.
  const stopped = $derived(
    medications
      .filter((m) => !m.active)
      .slice()
      .sort((a, b) => {
        if (a.end_date && b.end_date) return b.end_date.localeCompare(a.end_date) || b.id - a.id;
        if (a.end_date) return -1;
        if (b.end_date) return 1;
        return b.id - a.id;
      }),
  );

  async function load() {
    loading = true;
    error = '';
    try {
      const resp = await listMedications({ status: 'all', limit: 500 });
      medications = resp.medications;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load medications';
    } finally {
      loading = false;
    }
  }

  function resetForm() {
    formName = '';
    formCategory = 'prescription';
    formDose = '';
    formUnit = '';
    formFrequency = '';
    formStart = localDateISO();
    formGeneric = '';
    formRoute = '';
    formPrescriber = '';
    formPharmacy = '';
    formReason = '';
    formNotes = '';
  }

  async function submit() {
    formError = '';
    saving = true;
    const supplement = formCategory === 'supplement';
    try {
      await createMedication({
        name: formName.trim(),
        category: formCategory,
        dose: formDose || undefined,
        dose_unit: formUnit || undefined,
        frequency: formFrequency || undefined,
        start_date: formStart || undefined,
        generic_name: formGeneric || undefined,
        route: formRoute || undefined,
        prescriber: supplement ? undefined : formPrescriber || undefined,
        pharmacy: supplement ? undefined : formPharmacy || undefined,
        reason: formReason || undefined,
        notes: formNotes || undefined,
      });
      resetForm();
      formOpen = false;
      await load();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  let stopTarget: Medication | null = $state(null);
  let stopOpen = $state(false);
  let stopDate = $state(localDateISO());

  function askStop(m: Medication) {
    stopTarget = m;
    stopDate = localDateISO();
    stopOpen = true;
  }

  async function performStop() {
    const target = stopTarget;
    if (!target) return;
    try {
      await discontinueMedication(target.id, stopDate);
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to stop medication');
      return;
    }
    stopTarget = null;
    await load();
  }

  const deleteTarget = createConfirm<number>();

  async function performDelete() {
    if (deleteTarget.target == null) return;
    const id = deleteTarget.target;
    deleteTarget.cancel();
    try {
      await deleteMedication(id);
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to delete medication');
      return;
    }
    await load();
  }

  function rowActions(m: Medication): KebabItem[] {
    const items: KebabItem[] = [
      { label: 'Edit', href: `${base}/health/medications/detail?id=${m.id}` },
    ];
    if (m.active) items.push({ label: 'Stop', onSelect: () => askStop(m) });
    items.push({ label: 'Delete', danger: true, onSelect: () => deleteTarget.ask(m.id) });
    return items;
  }

  function doseText(m: Medication): string {
    if (!m.dose) return '';
    return m.dose_unit ? `${m.dose} ${m.dose_unit}` : m.dose;
  }

  function frequencyText(m: Medication): string {
    return (m.frequency ?? '').replaceAll('_', ' ');
  }

  function showGeneric(m: Medication): boolean {
    return !!m.generic_name && m.generic_name.trim().toLowerCase() !== m.name.trim().toLowerCase();
  }

  onMount(load);
</script>

{#snippet nameCell(m: Medication)}
  <td class="name cell-primary">
    <span class="name-line">
      <span class="title-card">{m.name}</span>
      <Badge variant="neutral" tone="subtle" uppercase={false} size="sm"
        >{categoryBadge[m.category] ?? m.category}</Badge
      >
    </span>
    {#if showGeneric(m)}
      <span class="caption generic">{m.generic_name}</span>
    {/if}
  </td>
{/snippet}

{#snippet actionsCell(m: Medication)}
  <td class="col-actions">
    <KebabMenu ariaLabel="Actions for {m.name}" items={rowActions(m)} />
  </td>
{/snippet}

{#snippet activeTable(rows: Medication[], label: string, withPrescriber: boolean)}
  <div class="table-scroll table--cards">
    <table class="table" aria-label={label}>
      <thead>
        <tr>
          <th>Name</th>
          <th>Dose</th>
          <th>Frequency</th>
          <th>Started</th>
          {#if withPrescriber}<th>Prescriber</th>{/if}
          <th class="col-actions"></th>
        </tr>
      </thead>
      <tbody>
        {#each rows as m (m.id)}
          <tr>
            {@render nameCell(m)}
            <td class:card-hidden={!m.dose}>{doseText(m)}</td>
            <td class:card-hidden={!m.frequency}>{frequencyText(m)}</td>
            <td class:card-hidden={!m.start_date}><Timestamp value={m.start_date} empty="" /></td>
            {#if withPrescriber}
              <td class:card-hidden={!m.prescriber}>{m.prescriber ?? ''}</td>
            {/if}
            {@render actionsCell(m)}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/snippet}

<LoadState {loading} {error} hasData={medications.length > 0} onRetry={load}>
  {#snippet header()}
    <PageHeader title="Medications">
      {#snippet actions()}
        <Button onclick={() => (formOpen = !formOpen)}>
          {formOpen ? 'Cancel' : '+ Add'}
        </Button>
      {/snippet}
    </PageHeader>

    {#if formOpen}
      <div class="entry-form">
        <Card>
          <FormStack as="form" onsubmit={submit}>
            <FormGrid>
              <Field label="Name" required>
                <Input type="text" bind:value={formName} required placeholder="Atorvastatin" />
              </Field>
              <Field label="Category">
                <Select
                  value={formCategory}
                  options={categoryOptions}
                  onValueChange={(v) => (formCategory = v as MedicationCategory)}
                  ariaLabel="Category"
                  fullWidth
                />
              </Field>
              <Field label="Dose">
                <Input type="text" bind:value={formDose} placeholder="20" />
              </Field>
              <Field label="Unit">
                <Input type="text" bind:value={formUnit} placeholder="mg" />
              </Field>
              <Field label="Frequency">
                <AutocompleteInput bind:value={formFrequency} options={frequencyOptions} />
              </Field>
              <Field label="Start date">
                <Input type="date" bind:value={formStart} />
              </Field>
            </FormGrid>
            <div class="advanced">
              <Disclosure summary="More fields">
                <FormGrid>
                  <Field label="Generic name">
                    <Input type="text" bind:value={formGeneric} />
                  </Field>
                  <Field label="Route">
                    <Select
                      value={formRoute}
                      options={routeOptions}
                      onValueChange={(v) => (formRoute = v)}
                      ariaLabel="Route"
                      fullWidth
                    />
                  </Field>
                  {#if formCategory !== 'supplement'}
                    <Field label="Prescriber">
                      <Input type="text" bind:value={formPrescriber} />
                    </Field>
                    <Field label="Pharmacy">
                      <Input type="text" bind:value={formPharmacy} />
                    </Field>
                  {/if}
                  <Field label="Reason">
                    <Input type="text" bind:value={formReason} />
                  </Field>
                </FormGrid>
                <Field label="Notes" class="notes">
                  <TextArea bind:value={formNotes} rows={2} />
                </Field>
              </Disclosure>
            </div>
            {#if formError}
              <Alert appearance="inline" variant="danger" role="alert">{formError}</Alert>
            {/if}
            <div class="form-actions">
              <Button variant="primary" type="submit" loading={saving}>Save</Button>
            </div>
          </FormStack>
        </Card>
      </div>
    {/if}
  {/snippet}

  {#if active.length === 0}
    <EmptyState>No current medications recorded.</EmptyState>
  {:else}
    <section class="section">
      <h2 class="micro-label">Prescriptions</h2>
      {#if prescriptions.length === 0}
        <EmptyState size="sm">No current prescriptions.</EmptyState>
      {:else}
        {@render activeTable(prescriptions, 'Prescriptions', true)}
      {/if}
    </section>
    <section class="section">
      <h2 class="micro-label">OTC and supplements</h2>
      {#if otherActive.length === 0}
        <EmptyState size="sm">No current OTC medications or supplements.</EmptyState>
      {:else}
        {@render activeTable(otherActive, 'OTC and supplements', false)}
      {/if}
    </section>
  {/if}

  {#if stopped.length > 0}
    <section class="section">
      <Disclosure summary="Stopped ({stopped.length})">
        <div class="table-scroll table--cards">
          <table class="table" aria-label="Stopped">
            <thead>
              <tr>
                <th>Name</th>
                <th>Dose</th>
                <th>Started</th>
                <th>Ended</th>
                <th class="col-actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each stopped as m (m.id)}
                <tr>
                  {@render nameCell(m)}
                  <td class:card-hidden={!m.dose}>{doseText(m)}</td>
                  <td class:card-hidden={!m.start_date}
                    ><Timestamp value={m.start_date} empty="" /></td
                  >
                  <td class="ended"><Timestamp value={m.end_date} empty="—" /></td>
                  {@render actionsCell(m)}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </Disclosure>
    </section>
  {/if}
</LoadState>

<FormModal
  bind:open={stopOpen}
  title="Stop medication"
  description={stopTarget ? `Record the day you stopped ${stopTarget.name}.` : undefined}
  submitLabel="Stop"
  busyLabel="Stopping…"
  canSubmit={!!stopDate}
  onSubmit={performStop}
  onCancel={() => (stopOpen = false)}
>
  <Field label="End date">
    <Input type="date" bind:value={stopDate} required />
  </Field>
</FormModal>

<ConfirmDialog
  bind:open={deleteTarget.open}
  title="Delete medication"
  message="Are you sure you want to delete this medication? This cannot be undone. To record that you stopped taking it, use Stop instead."
  confirmLabel="Delete"
  confirmVariant="danger"
  onConfirm={performDelete}
/>

<style>
  .entry-form {
    margin-bottom: var(--space-4);
  }
  /* The notes sit under the grid inside the disclosure, where no gap reaches. */
  .advanced :global(.notes) {
    margin-top: var(--space-1);
  }
  .section {
    margin-bottom: var(--space-6);
  }
  .section h2 {
    margin: 0 0 var(--space-2);
  }
  .name-line {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }
  .generic {
    display: block;
  }
  td.ended {
    color: var(--text-muted);
  }
</style>
