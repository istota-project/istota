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
  import { Select, ConfirmDialog, type SelectOption } from '$lib/components/ui';

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

<div class="header">
  <h1>Immunization detail</h1>
  <div class="actions">
    <a class="btn" href="{base}/health/immunizations">Back</a>
    {#if immunization}
      <a
        class="btn"
        href="{base}/health/immunizations/vaccine?name={encodeURIComponent(immunization.name)}"
      >
        View all {immunization.name}
      </a>
      <button class="btn danger" type="button" onclick={() => (confirmDelete = true)}>Delete</button
      >
    {/if}
  </div>
</div>

{#if loading}
  <div class="loading">Loading…</div>
{:else if error}
  <div class="msg error">{error}</div>
{:else if immunization}
  <form class="card form" onsubmit={save}>
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
        <Select
          value={immunization.route ?? ''}
          options={routeOptions}
          onValueChange={(v) => {
            if (immunization) immunization.route = v || null;
          }}
          ariaLabel="Route"
          fullWidth
        />
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
    <div class="meta">
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
      <a class="card linked-card" href="{base}/health/history/encounter?id={encounter.id}">
        <div class="card-head">
          <span class="badge type-other">{encounter.encounter_type}</span>
          <span class="date">{encounter.encounter_date}</span>
        </div>
        {#if encounter.provider || encounter.facility}
          <div class="muted">
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
    gap: 1rem;
    margin-bottom: 1rem;
    flex-wrap: wrap;
  }
  h1 {
    font-size: var(--text-lg, 1.05rem);
    font-weight: 500;
    margin: 0;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .btn {
    padding: 0.4rem 0.85rem;
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    color: var(--text-primary);
    text-decoration: none;
    font: inherit;
    font-size: var(--text-sm);
    cursor: pointer;
    line-height: 1.2;
  }
  .btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .btn:hover:not(:disabled) {
    background: var(--surface-raised);
  }
  .btn.primary {
    border-color: #7aa3d8;
    color: #7aa3d8;
  }
  .btn.danger {
    color: var(--text-muted);
  }
  .btn.danger:hover:not(:disabled) {
    color: #e88;
  }

  .card {
    padding: 0.85rem 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.65rem;
  }
  .form .row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr));
    gap: 0.65rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: var(--text-sm);
    min-width: 0;
  }
  label.full {
    grid-column: 1 / -1;
  }
  label > span {
    color: var(--text-muted);
    font-size: var(--text-xs);
  }
  input,
  textarea {
    padding: 0.3rem 0.5rem;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: 0.3rem;
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    box-sizing: border-box;
  }
  textarea {
    resize: vertical;
    font-family: inherit;
  }
  .form-actions {
    display: flex;
    justify-content: flex-end;
  }
  .meta {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
  .msg {
    font-size: var(--text-sm);
    padding: 0.4rem 0.6rem;
    border-radius: 0.3rem;
  }
  .msg.error {
    background: rgba(204, 102, 102, 0.1);
    color: #e88;
  }
  .empty {
    color: var(--text-dim);
    font-size: var(--text-base);
    padding: 2rem 1rem;
    text-align: center;
  }

  .linked {
    margin-top: 1.25rem;
  }
  .linked h2 {
    margin: 0 0 0.5rem;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-dim);
    font-weight: 500;
  }
  .linked-card {
    display: block;
    text-decoration: none;
    color: var(--text-primary);
  }
  .linked-card:hover {
    border-color: #555;
  }
  .card-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.35rem;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.1rem 0.5rem;
    border-radius: var(--radius-pill);
    font-weight: 500;
    background: hsla(220, 8%, 60%, 0.18);
    color: var(--text-muted);
  }
  .date {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
  .muted {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  /* Light theme overrides — dark rules above untouched. */
  :global(:root[data-theme='light']) .btn.primary {
    border-color: #2563b0;
    color: #2563b0;
  }
  :global(:root[data-theme='light']) .btn.danger:hover:not(:disabled) {
    color: #c0271d;
  }
  :global(:root[data-theme='light']) .linked-card:hover {
    border-color: var(--border-default);
  }
  :global(:root[data-theme='light']) .msg.error {
    color: #c0271d;
  }
</style>
