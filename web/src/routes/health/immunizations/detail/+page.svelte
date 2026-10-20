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
    type HealthDocument,
    type Immunization,
  } from '$lib/api';
  import {
    Badge,
    Button,
    Card,
    ConfirmDialog,
    EmptyState,
    Field,
    FormGrid,
    FormStack,
    Select,
    type SelectOption,
    Input,
    TextArea,
    LoadState,
    PageHeader,
  } from '$lib/components/ui';
  import DocumentList from '$lib/components/health/DocumentList.svelte';

  const routeOptions: SelectOption[] = [
    { value: '', label: '' },
    { value: 'IM', label: 'IM' },
    { value: 'SC', label: 'SC' },
    { value: 'oral', label: 'Oral' },
    { value: 'nasal', label: 'Nasal' },
  ];

  let id = $derived(Number(page.url.searchParams.get('id')) || 0);
  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let formError = $state('');
  let immunization: Immunization | null = $state(null);
  let encounter: Encounter | null = $state(null);
  let documents: HealthDocument[] = $state([]);

  async function load() {
    if (!id) return;
    loading = true;
    error = '';
    try {
      const out = await getImmunization(id);
      immunization = out.immunization;
      encounter = out.encounter;
      documents = out.documents ?? [];
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

  let confirmDelete = $state(false);

  async function remove() {
    if (!immunization) return;
    confirmDelete = false;
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

{#snippet doseActions()}
  {#if immunization}
    <Button href="{base}/health/immunizations/vaccine?name={encodeURIComponent(immunization.name)}">
      View all {immunization.name}
    </Button>
    <Button variant="danger" onclick={() => (confirmDelete = true)}>Delete</Button>
  {/if}
{/snippet}

<LoadState {loading} {error} empty={!immunization}>
  {#snippet header()}
    <PageHeader
      title="Immunization detail"
      back={{ href: `${base}/health/immunizations`, label: 'Immunizations' }}
      align="start"
      actions={immunization ? doseActions : undefined}
    />
  {/snippet}
  {#snippet emptyState()}
    <EmptyState>Immunization not found.</EmptyState>
  {/snippet}
  {#if immunization}
    <Card as="div">
      <FormStack as="form" onsubmit={save}>
        <FormGrid>
          <Field label="Vaccine name">
            <Input type="text" bind:value={immunization.name} required />
          </Field>
          <Field label="Date given">
            <Input type="date" bind:value={immunization.date_given} required />
          </Field>
          <Field label="Product">
            <Input
              type="text"
              value={immunization.product_name ?? ''}
              oninput={(e) =>
                (immunization!.product_name = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="Manufacturer">
            <Input
              type="text"
              value={immunization.manufacturer ?? ''}
              oninput={(e) =>
                (immunization!.manufacturer = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="Dose label">
            <Input
              type="text"
              value={immunization.dose_label ?? ''}
              oninput={(e) =>
                (immunization!.dose_label = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="Lot number">
            <Input
              type="text"
              value={immunization.lot_number ?? ''}
              oninput={(e) =>
                (immunization!.lot_number = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="Route">
            <Select
              value={immunization.route ?? ''}
              options={routeOptions}
              onValueChange={(v) => {
                if (immunization) immunization.route = v || null;
              }}
              ariaLabel="Route"
              fullWidth
            />
          </Field>
          <Field label="Site">
            <Input
              type="text"
              value={immunization.site ?? ''}
              oninput={(e) =>
                (immunization!.site = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="Administered by">
            <Input
              type="text"
              value={immunization.administered_by ?? ''}
              oninput={(e) =>
                (immunization!.administered_by =
                  (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="Facility">
            <Input
              type="text"
              value={immunization.facility ?? ''}
              oninput={(e) =>
                (immunization!.facility = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="CVX code">
            <Input
              type="text"
              value={immunization.cvx_code ?? ''}
              oninput={(e) =>
                (immunization!.cvx_code = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
        </FormGrid>
        <Field label="Notes">
          <TextArea
            rows={3}
            value={immunization.notes ?? ''}
            oninput={(e) =>
              (immunization!.notes = (e.currentTarget as HTMLTextAreaElement).value || null)}
          />
        </Field>
        <div class="caption">
          Source: {immunization.source}
          {#if immunization.created_at}
            · Created: {immunization.created_at}
          {/if}
        </div>
        {#if formError}
          <div class="banner error">{formError}</div>
        {/if}
        <div class="form-actions">
          <Button variant="primary" type="submit" loading={saving}>Save</Button>
        </div>
      </FormStack>
    </Card>

    <section class="linked">
      <h2 class="micro-label">Proof of immunization</h2>
      <DocumentList entityType="immunization" entityId={immunization.id} {documents} />
    </section>

    {#if encounter}
      <section class="linked">
        <h2 class="micro-label">Linked encounter</h2>
        <Card href="{base}/health/history/encounter?id={encounter.id}">
          <div class="card-head">
            <Badge>{encounter.encounter_type}</Badge>
            <span class="date caption">{encounter.encounter_date}</span>
          </div>
          {#if encounter.provider || encounter.facility}
            <div class="caption">
              {encounter.provider || ''}{encounter.provider && encounter.facility
                ? ' · '
                : ''}{encounter.facility || ''}
            </div>
          {/if}
        </Card>
      </section>
    {/if}
  {/if}
</LoadState>

<ConfirmDialog
  bind:open={confirmDelete}
  title="Delete immunization"
  message="Are you sure you want to delete this immunization? This cannot be undone."
  confirmLabel="Delete"
  onConfirm={remove}
/>

<style>
  .linked {
    margin-top: 1.25rem;
  }
  .linked h2 {
    margin: 0 0 var(--space-2);
  }
  .linked .card-head {
    align-items: center;
  }
</style>
