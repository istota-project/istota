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
    ConfirmDialog,
    Field,
    Select,
    type SelectOption,
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

{#if !loading}
  <!-- Held back while loading so the pane shows nothing but the centered
       loading message, rather than centering it in the space left under
       this header. -->
  <div class="header">
    <h1>Immunization detail</h1>
    <div class="actions">
      <Button href="{base}/health/immunizations">Back</Button>
      {#if immunization}
        <Button
          href="{base}/health/immunizations/vaccine?name={encodeURIComponent(immunization.name)}"
        >
          View all {immunization.name}
        </Button>
        <Button variant="danger" onclick={() => (confirmDelete = true)}>Delete</Button>
      {/if}
    </div>
  </div>
{/if}

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg error">{error}</div>
{:else if immunization}
  <form class="card form" onsubmit={save}>
    <div class="row">
      <Field label="Vaccine name">
        <input type="text" bind:value={immunization.name} required />
      </Field>
      <Field label="Date given">
        <input type="date" bind:value={immunization.date_given} required />
      </Field>
      <Field label="Product">
        <input
          type="text"
          value={immunization.product_name ?? ''}
          oninput={(e) =>
            (immunization!.product_name = (e.currentTarget as HTMLInputElement).value || null)}
        />
      </Field>
      <Field label="Manufacturer">
        <input
          type="text"
          value={immunization.manufacturer ?? ''}
          oninput={(e) =>
            (immunization!.manufacturer = (e.currentTarget as HTMLInputElement).value || null)}
        />
      </Field>
      <Field label="Dose label">
        <input
          type="text"
          value={immunization.dose_label ?? ''}
          oninput={(e) =>
            (immunization!.dose_label = (e.currentTarget as HTMLInputElement).value || null)}
        />
      </Field>
      <Field label="Lot number">
        <input
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
        <input
          type="text"
          value={immunization.site ?? ''}
          oninput={(e) =>
            (immunization!.site = (e.currentTarget as HTMLInputElement).value || null)}
        />
      </Field>
      <Field label="Administered by">
        <input
          type="text"
          value={immunization.administered_by ?? ''}
          oninput={(e) =>
            (immunization!.administered_by = (e.currentTarget as HTMLInputElement).value || null)}
        />
      </Field>
      <Field label="Facility">
        <input
          type="text"
          value={immunization.facility ?? ''}
          oninput={(e) =>
            (immunization!.facility = (e.currentTarget as HTMLInputElement).value || null)}
        />
      </Field>
      <Field label="CVX code">
        <input
          type="text"
          value={immunization.cvx_code ?? ''}
          oninput={(e) =>
            (immunization!.cvx_code = (e.currentTarget as HTMLInputElement).value || null)}
        />
      </Field>
    </div>
    <Field label="Notes" class="full">
      <textarea
        rows="3"
        value={immunization.notes ?? ''}
        oninput={(e) =>
          (immunization!.notes = (e.currentTarget as HTMLTextAreaElement).value || null)}
      ></textarea>
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
  </form>

  <section class="linked">
    <h2>Proof of immunization</h2>
    <DocumentList entityType="immunization" entityId={immunization.id} {documents} />
  </section>

  {#if encounter}
    <section class="linked">
      <h2 class="micro-label">Linked encounter</h2>
      <a class="card linked-card" href="{base}/health/history/encounter?id={encounter.id}">
        <div class="card-head">
          <span class="badge type-other">{encounter.encounter_type}</span>
          <span class="date">{encounter.encounter_date}</span>
        </div>
        {#if encounter.provider || encounter.facility}
          <div class="caption">
            {encounter.provider || ''}{encounter.provider && encounter.facility
              ? ' · '
              : ''}{encounter.facility || ''}
          </div>
        {/if}
      </a>
    </section>
  {/if}
{:else}
  <div class="empty">Immunization not found.</div>
{/if}

<ConfirmDialog
  bind:open={confirmDelete}
  title="Delete immunization"
  message="Are you sure you want to delete this immunization? This cannot be undone."
  confirmLabel="Delete"
  onConfirm={remove}
/>

<style>
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
    flex-wrap: wrap;
  }
  h1 {
    font-size: var(--text-lg);
    font-weight: 500;
    margin: 0;
  }
  .actions {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
  }

  .card {
    padding: var(--space-3) var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .form .row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr));
    gap: var(--space-3);
  }
  /* Scoped to this page's own form: a bare :global(.field.full) leaks
     app-wide, which is the hazard the money report pages already hit. */
  .form :global(.field.full) {
    grid-column: 1 / -1;
  }
  input,
  textarea {
    padding: var(--space-1) var(--space-2);
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    box-sizing: border-box;
  }
  textarea {
    resize: vertical;
    font-family: inherit;
  }
  .linked {
    margin-top: 1.25rem;
  }
  .linked h2 {
    margin: 0 0 var(--space-2);
  }
  .linked-card {
    display: block;
    text-decoration: none;
    color: var(--text-primary);
  }
  .linked-card:hover {
    border-color: var(--border-hover);
  }
  .card-head {
    align-items: center;
    margin-bottom: var(--space-2);
  }
  .date {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
</style>
