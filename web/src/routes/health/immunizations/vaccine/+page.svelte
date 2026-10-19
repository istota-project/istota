<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import {
    getImmunizationCoverage,
    getImmunizationExplainer,
    listImmunizations,
    listImmunizationRefs,
    type CoverageEntry,
    type Immunization,
    type ImmunizationExplainer,
    type ImmunizationRef,
    type ImmunizationStatus,
  } from '$lib/api';
  import {
    Badge,
    Button,
    Card,
    EmptyState,
    KebabMenu,
    LoadState,
    PageHeader,
    Timestamp,
  } from '$lib/components/ui';
  import { immunizationStatusLabel, immunizationStatusVariant } from '$lib/health/status';

  let name = $derived(page.url.searchParams.get('name') || '');
  let loading = $state(true);
  let error = $state('');
  let ref: ImmunizationRef | null = $state(null);
  let entry: CoverageEntry | null = $state(null);
  let history: Immunization[] = $state([]);
  let explainer: ImmunizationExplainer | null = $state(null);
  let explainerLoading = $state(false);

  let loadToken = 0;

  async function load() {
    if (!name) return;
    const token = ++loadToken;
    loading = true;
    error = '';
    explainer = null;
    try {
      const [refResp, cov, hist] = await Promise.all([
        listImmunizationRefs(),
        getImmunizationCoverage(),
        listImmunizations({ name, limit: 200 }),
      ]);
      if (token !== loadToken) return;
      ref = refResp.refs.find((r) => r.name === name) ?? null;
      entry = cov.coverage.find((c) => c.name === name) ?? null;
      history = hist.immunizations;
    } catch (e) {
      if (token !== loadToken) return;
      error = e instanceof Error ? e.message : 'Failed to load';
    } finally {
      if (token === loadToken) loading = false;
    }

    if (entry && token === loadToken) {
      explainerLoading = true;
      try {
        const next = await getImmunizationExplainer(name);
        if (token === loadToken) explainer = next;
      } catch {
        // Leave whatever was last successfully loaded in place.
      } finally {
        if (token === loadToken) explainerLoading = false;
      }
    }
  }

  $effect(() => {
    if (name) load();
  });
</script>

<LoadState {loading} {error} empty={!ref}>
  {#snippet header()}
    <PageHeader
      title={ref?.display_name || name}
      back={{ href: `${base}/health/immunizations`, label: 'Immunizations' }}
    />
  {/snippet}
  {#snippet emptyState()}
    <EmptyState>Unknown vaccine "{name}". It may not be in the canonical reference list.</EmptyState
    >
  {/snippet}
  {#if ref}
    <div class="vaccine">
      <Card class="coverage-card">
        {#if entry}
          <div class="status-row">
            <Badge variant={immunizationStatusVariant(entry.status)}
              >{immunizationStatusLabel(entry.status)}</Badge
            >
            <span class="caption">{ref.category} · {ref.schedule}</span>
          </div>
          <dl class="kv-stacked">
            <div>
              <dt>Last given</dt>
              <dd><Timestamp value={entry.last_given} empty="—" /></dd>
            </div>
            <div>
              <dt>Doses recorded</dt>
              <dd>{entry.dose_count}</dd>
            </div>
            <div>
              <dt>Next due</dt>
              <dd><Timestamp value={entry.next_due} empty="—" /></dd>
            </div>
            {#if entry.days_until_due !== null}
              <div>
                <dt>
                  {entry.days_until_due < 0 ? 'Days overdue' : 'Days until due'}
                </dt>
                <dd>{Math.abs(entry.days_until_due)}</dd>
              </div>
            {/if}
          </dl>
        {/if}
        {#if ref.description}
          <p class="ref-description">{ref.description}</p>
        {/if}
        {#if ref.typical_age_range}
          <p class="caption">Typical age range: {ref.typical_age_range}</p>
        {/if}
      </Card>

      {#if explainerLoading}
        <Card class="coverage-card explainer-placeholder">
          <h2 class="micro-label">About this vaccine</h2>
          <p class="caption">Loading…</p>
        </Card>
      {:else if explainer && explainer.summary}
        <details class="explainer">
          <summary>
            <span class="label micro-label">About this vaccine</span>
            <span class="chev" aria-hidden="true">›</span>
          </summary>
          <div class="content">
            <p class="summary">{explainer.summary}</p>
            {#if explainer.why_it_matters.length > 0}
              <h3 class="micro-label">Why it matters</h3>
              <ul>
                {#each explainer.why_it_matters as item (item)}
                  <li>{item}</li>
                {/each}
              </ul>
            {/if}
            {#if explainer.disclaimer}
              <p class="disclaimer">{explainer.disclaimer}</p>
            {/if}
          </div>
        </details>
      {/if}

      <section class="history">
        <h2 class="micro-label">Dose history</h2>
        {#if history.length === 0}
          <EmptyState size="sm">No doses recorded yet.</EmptyState>
        {:else}
          <div class="table-scroll">
            <table class="table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Product</th>
                  <th>Dose label</th>
                  <th>Facility</th>
                  <th>Notes</th>
                  <th class="col-actions"></th>
                </tr>
              </thead>
              <tbody>
                {#each history as i (i.id)}
                  <tr>
                    <td><Timestamp value={i.date_given} empty="—" /></td>
                    <td>{i.product_name || '—'}</td>
                    <td>{i.dose_label || '—'}</td>
                    <td>{i.facility || '—'}</td>
                    <td class="notes cell-truncate">{i.notes || '—'}</td>
                    <td class="col-actions">
                      <KebabMenu
                        ariaLabel="Immunization actions"
                        items={[
                          { label: 'Edit', href: `${base}/health/immunizations/detail?id=${i.id}` },
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
    </div>
  {/if}
</LoadState>

<style>
  .vaccine :global(.coverage-card) {
    margin-bottom: var(--space-4);
  }
  .status-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  dd {
    color: var(--text-primary);
  }
  .ref-description {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    line-height: var(--leading-relaxed);
    max-width: 75ch;
  }

  /* A disclosure drawn as a card until the Disclosure component takes it: the
     padding lives on the summary and the content rather than on the
     <details>, or the click target would stop short of the panel's edges. */
  details.explainer {
    margin-bottom: var(--space-4);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
  }
  details.explainer > summary {
    list-style: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    /* The card's padding, so the collapsed panel is the same box as the
       coverage card above it. */
    padding: var(--space-4);
    user-select: none;
  }
  details.explainer > summary::-webkit-details-marker {
    display: none;
  }
  details.explainer > summary .chev {
    color: var(--text-dim);
    font-size: var(--text-lg);
    line-height: var(--leading-none);
    transition: transform var(--transition-fast);
  }
  details.explainer[open] > summary .chev {
    transform: rotate(90deg);
  }
  details.explainer:hover > summary .label,
  details.explainer:hover > summary .chev {
    color: var(--text-muted);
  }
  details.explainer > .content {
    /* No top padding: the summary's own bottom padding already separates the
		   label from the body, so adding more would double it once open. */
    padding: 0 var(--space-4) var(--space-3);
  }
  .explainer h3 {
    margin: var(--space-3) 0 var(--space-2);
  }
  .explainer .summary {
    margin: 0;
    font-size: var(--text-base);
    color: var(--text-secondary);
    line-height: var(--leading-relaxed);
    max-width: 75ch;
  }
  .explainer ul {
    margin: 0;
    padding-left: 1.1rem;
    font-size: var(--text-base);
    color: var(--text-secondary);
    line-height: var(--leading-relaxed);
  }
  .explainer li {
    margin: 0.2rem 0;
    /* Same measure as .summary above, applied to the item rather than the
		   list: the <ul>'s marker indent would otherwise eat into the width and
		   wrap each bullet a little earlier than the paragraph. */
    max-width: 75ch;
  }
  .explainer .disclaimer {
    margin: var(--space-3) 0 0;
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-style: italic;
  }
  .vaccine :global(.explainer-placeholder) {
    opacity: 0.7;
  }

  .history h2 {
    margin: 0 0 var(--space-2);
  }
  td.notes {
    max-width: 260px;
    color: var(--text-muted);
  }

  /* Light theme overrides — dark rules above untouched. */
</style>
