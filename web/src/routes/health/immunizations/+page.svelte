<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getImmunizationCoverage,
    listImmunizations,
    listImmunizationRefs,
    createImmunization,
    deleteImmunization,
    type CoverageEntry,
    type Immunization,
    type ImmunizationRef,
    type ImmunizationStatus,
  } from '$lib/api';
  import {
    Badge,
    Button,
    ConfirmDialog,
    KebabMenu,
    Select,
    type SelectOption,
  } from '$lib/components/ui';
  import { immunizationStatusLabel, immunizationStatusVariant } from '$lib/health/status';

  let loading = $state(true);
  let error = $state('');
  let coverage: CoverageEntry[] = $state([]);
  let other: CoverageEntry[] = $state([]);
  let history: Immunization[] = $state([]);
  let refs: ImmunizationRef[] = $state([]);
  let nameFilter = $state('');

  let formOpen = $state(false);
  let formName = $state('Influenza');
  let formDate = $state(new Date().toISOString().slice(0, 10));
  let formProduct = $state('');
  let formFacility = $state('');
  let formLot = $state('');
  let formRoute = $state('');
  let formSite = $state('');
  let formNotes = $state('');
  let saving = $state(false);
  let formError = $state('');

  const vaccineOptions: SelectOption[] = $derived(
    refs.map((r) => ({ value: r.name, label: r.display_name })),
  );
  const routeOptions: SelectOption[] = [
    { value: '', label: '' },
    { value: 'IM', label: 'IM' },
    { value: 'SC', label: 'SC' },
    { value: 'oral', label: 'Oral' },
    { value: 'nasal', label: 'Nasal' },
  ];

  async function load() {
    loading = true;
    error = '';
    try {
      const [cov, hist, refResp] = await Promise.all([
        getImmunizationCoverage(),
        listImmunizations({ name: nameFilter || undefined, limit: 500 }),
        listImmunizationRefs(),
      ]);
      coverage = cov.coverage;
      other = cov.other;
      history = hist.immunizations;
      refs = refResp.refs;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load immunizations';
    } finally {
      loading = false;
    }
  }

  async function submit(e: Event) {
    e.preventDefault();
    formError = '';
    saving = true;
    try {
      await createImmunization({
        name: formName,
        date_given: formDate,
        product_name: formProduct || undefined,
        facility: formFacility || undefined,
        lot_number: formLot || undefined,
        route: formRoute || undefined,
        site: formSite || undefined,
        notes: formNotes || undefined,
      });
      formProduct = '';
      formFacility = '';
      formLot = '';
      formRoute = '';
      formSite = '';
      formNotes = '';
      formOpen = false;
      await load();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  let deleteTargetId: number | null = $state(null);

  async function performDeleteRow() {
    if (deleteTargetId == null) return;
    const id = deleteTargetId;
    deleteTargetId = null;
    try {
      await deleteImmunization(id);
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to delete';
    }
  }

  function formatDate(iso: string | null): string {
    if (!iso) return '—';
    try {
      const d = new Date(iso + (iso.includes('T') ? '' : 'T00:00:00'));
      return d.toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return iso;
    }
  }

  const statusOrder: Record<ImmunizationStatus, number> = {
    overdue: 0,
    expired: 0,
    due_soon: 1,
    series_incomplete: 2,
    up_to_date: 3,
    never_recorded: 4,
    risk_based: 5,
    recorded: 6,
  };

  const visibleCoverage = $derived(
    coverage
      .filter((c) => c.category !== 'risk_based')
      .slice()
      .sort(
        (a, b) =>
          (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9) ||
          a.display_name.localeCompare(b.display_name),
      ),
  );

  const riskBased = $derived(coverage.filter((c) => c.category === 'risk_based'));
  let riskOpen = $state(false);

  onMount(load);
</script>

{#if !loading}
  <!-- Held back while loading so the pane shows nothing but the centered
       loading message, rather than centering it in the space left under
       this header. -->
  <div class="header">
    <h1>Immunizations</h1>
    <div class="actions">
      <Button onclick={() => (formOpen = !formOpen)}>
        {formOpen ? 'Cancel' : '+ Log dose'}
      </Button>
      <Button href="{base}/health/immunizations/import">Import</Button>
    </div>
  </div>
{/if}

{#if formOpen}
  <form class="quick-form" onsubmit={submit}>
    <div class="row">
      <label>
        <span>Vaccine</span>
        <Select
          value={formName}
          options={vaccineOptions}
          onValueChange={(v) => (formName = v)}
          ariaLabel="Vaccine"
          fullWidth
        />
      </label>
      <label>
        <span>Date</span>
        <input type="date" bind:value={formDate} required />
      </label>
      <label>
        <span>Product</span>
        <input type="text" bind:value={formProduct} placeholder="Fluzone Quadrivalent" />
      </label>
      <label>
        <span>Facility</span>
        <input type="text" bind:value={formFacility} placeholder="CVS Pharmacy" />
      </label>
    </div>
    <details class="advanced">
      <summary>More fields</summary>
      <div class="row">
        <label>
          <span>Lot number</span>
          <input type="text" bind:value={formLot} />
        </label>
        <label>
          <span>Route</span>
          <Select
            value={formRoute}
            options={routeOptions}
            onValueChange={(v) => (formRoute = v)}
            ariaLabel="Route"
            fullWidth
          />
        </label>
        <label>
          <span>Site</span>
          <input type="text" bind:value={formSite} placeholder="left deltoid" />
        </label>
      </div>
      <label class="full">
        <span>Notes</span>
        <textarea bind:value={formNotes} rows="2"></textarea>
      </label>
    </details>
    {#if formError}
      <div class="banner error">{formError}</div>
    {/if}
    <div class="form-actions">
      <Button variant="primary" type="submit" loading={saving}>Save</Button>
    </div>
  </form>
{/if}

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="banner error">{error}</div>
{:else}
  <section class="coverage">
    <h2>Coverage</h2>
    <ul class="cards card-grid">
      {#each visibleCoverage as c (c.name)}
        <li>
          <a
            class="card interactive"
            href="{base}/health/immunizations/vaccine?name={encodeURIComponent(c.name)}"
          >
            <span class="name">{c.display_name}</span>
            <Badge variant={immunizationStatusVariant(c.status)}
              >{immunizationStatusLabel(c.status)}</Badge
            >
            <div class="card-body">
              <div class="muted">
                {#if c.last_given}
                  Last: {formatDate(c.last_given)}{#if c.dose_count > 1}
                    · {c.dose_count} doses{/if}
                {:else}
                  No record
                {/if}
              </div>
              {#if c.next_due}
                <div class="muted">
                  Next due: {formatDate(c.next_due)}
                  {#if c.days_until_due !== null}
                    {#if c.days_until_due < 0}
                      ({-c.days_until_due}d overdue)
                    {:else}
                      (in {c.days_until_due}d)
                    {/if}
                  {/if}
                </div>
              {/if}
            </div>
          </a>
        </li>
      {/each}
    </ul>

    {#if riskBased.length > 0}
      <details class="risk-based" bind:open={riskOpen}>
        <summary>Risk-based vaccines ({riskBased.length})</summary>
        <ul class="cards card-grid">
          {#each riskBased as c (c.name)}
            <li>
              <a
                class="card interactive"
                href="{base}/health/immunizations/vaccine?name={encodeURIComponent(c.name)}"
              >
                <span class="name">{c.display_name}</span>
                <Badge variant={immunizationStatusVariant(c.status)}
                  >{immunizationStatusLabel(c.status)}</Badge
                >
                <div class="card-body">
                  <div class="muted">
                    {#if c.last_given}
                      Last: {formatDate(c.last_given)}
                    {:else}
                      Not recorded
                    {/if}
                  </div>
                </div>
              </a>
            </li>
          {/each}
        </ul>
      </details>
    {/if}

    {#if other.length > 0}
      <h3>Other recorded</h3>
      <ul class="cards card-grid">
        {#each other as c (c.name)}
          <li>
            <div class="card">
              <span class="name">{c.display_name}</span>
              <Badge variant="neutral">
                {c.dose_count} dose{c.dose_count > 1 ? 's' : ''}
              </Badge>
              <div class="card-body">
                <div class="muted">Last: {formatDate(c.last_given)}</div>
              </div>
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="history">
    <div class="history-head">
      <h2>History</h2>
      <input
        type="text"
        class="filter-input"
        placeholder="Filter by vaccine name"
        bind:value={nameFilter}
        onchange={load}
      />
    </div>
    {#if history.length === 0}
      <div class="empty small">No immunizations recorded yet.</div>
    {:else}
      <div class="table-scroll">
        <table class="grid">
          <thead>
            <tr>
              <th>Date</th>
              <th>Vaccine</th>
              <th>Product</th>
              <th>Dose label</th>
              <th>Facility</th>
              <th>Notes</th>
              <th class="row-actions"></th>
            </tr>
          </thead>
          <tbody>
            {#each history as i (i.id)}
              <tr>
                <td>{formatDate(i.date_given)}</td>
                <td>
                  <a
                    class="link"
                    href="{base}/health/immunizations/vaccine?name={encodeURIComponent(i.name)}"
                  >
                    {i.name}
                  </a>
                </td>
                <td>{i.product_name || '—'}</td>
                <td>{i.dose_label || '—'}</td>
                <td>{i.facility || '—'}</td>
                <td class="notes">{i.notes || '—'}</td>
                <td class="row-actions">
                  <KebabMenu
                    ariaLabel="Immunization actions"
                    items={[
                      { label: 'Edit', href: `${base}/health/immunizations/detail?id=${i.id}` },
                      {
                        label: 'Delete',
                        danger: true,
                        onSelect: () => (deleteTargetId = i.id),
                      },
                    ]}
                  />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
{/if}

<ConfirmDialog
  open={deleteTargetId != null}
  title="Delete immunization"
  message="Are you sure you want to delete this immunization? This cannot be undone."
  confirmLabel="Delete"
  onConfirm={performDeleteRow}
  onCancel={() => (deleteTargetId = null)}
/>

<style>
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }
  h1 {
    font-size: var(--text-lg);
    font-weight: 500;
    margin: 0;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .quick-form {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: 0.85rem 1rem;
    margin-bottom: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.65rem;
  }
  .quick-form .row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 0.65rem;
  }
  .quick-form label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: var(--text-sm);
    min-width: 0;
  }
  .quick-form label.full {
    grid-column: 1 / -1;
    margin-top: 0.25rem;
  }
  .quick-form label > span {
    color: var(--text-muted);
    font-size: var(--text-xs);
  }
  .quick-form input,
  .quick-form textarea {
    padding: 0.3rem 0.5rem;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: 0.3rem;
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    box-sizing: border-box;
    min-width: 0;
  }
  .quick-form textarea {
    resize: vertical;
    font-family: inherit;
  }
  .quick-form details.advanced > summary {
    color: var(--text-muted);
    font-size: var(--text-sm);
    cursor: pointer;
    user-select: none;
  }
  .form-actions {
    display: flex;
    justify-content: flex-end;
  }

  .coverage {
    margin-bottom: 1.25rem;
  }
  .coverage h2,
  .history h2 {
    margin: 0 0 0.5rem;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-dim);
    font-weight: 500;
  }
  .coverage h3 {
    margin: 0.85rem 0 0.4rem;
    font-size: var(--text-xs);
    font-weight: 500;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .cards {
    list-style: none;
    margin: 0;
    padding: 0;
    --card-min: 240px;
    --card-gap: 0.5rem;
    grid-auto-rows: 1fr;
  }
  .cards > li {
    display: flex;
  }
  .card {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.4rem;
    width: 100%;
    padding: 0.7rem 0.9rem;
    color: var(--text-primary);
  }
  .card .name {
    font-weight: 500;
    font-size: var(--text-sm);
    line-height: 1.35;
  }
  .card-body {
    margin-top: auto;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .muted {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .risk-based {
    margin-top: 0.75rem;
  }
  .risk-based > summary {
    cursor: pointer;
    font-size: var(--text-sm);
    color: var(--text-muted);
    padding: 0.35rem 0;
    user-select: none;
  }
  .risk-based[open] > summary {
    margin-bottom: 0.5rem;
  }

  .history-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
  }
  .filter-input {
    padding: 0.3rem 0.5rem;
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: 0.3rem;
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    max-width: 240px;
  }

  td.notes {
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-muted);
  }
  td.row-actions,
  th.row-actions {
    text-align: right;
    white-space: nowrap;
  }
  td.row-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.25rem;
  }
  a.link {
    color: var(--text-primary);
    text-decoration: none;
    border-bottom: 1px dotted var(--border-default);
  }
  a.link:hover {
    color: var(--accent-hover);
    border-bottom-color: var(--text-muted);
  }

  .empty {
    color: var(--text-dim);
    font-size: var(--text-base);
    padding: 2rem 1rem;
    text-align: center;
  }
  .empty.small {
    padding: 0.75rem 0;
    font-size: var(--text-sm);
  }
</style>
