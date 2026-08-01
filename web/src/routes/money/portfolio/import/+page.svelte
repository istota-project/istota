<script lang="ts">
  import { base } from '$app/paths';
  import { Field, Select } from '$lib/components/ui';
  import PortfolioUpload from '../PortfolioUpload.svelte';

  // One entry per importer-registry source; adding a source is a new row here,
  // not a new card. The value is the registry name the endpoint validates.
  const sources = [
    {
      value: 'fidelity-positions-csv',
      label: 'Fidelity CSV',
      description:
        'A Portfolio Positions export (Positions → Download). Each export becomes one snapshot.',
      dropLabel: 'Drop a Fidelity Portfolio Positions CSV here, or pick a file.',
      hint: 'Re-importing the same file is safe — duplicates are skipped.',
    },
    {
      value: 'fina-history-csv',
      label: 'FINA CSV',
      description:
        "fina's portfolio_history.csv database — the one-time migration. Every snapshot in the file is imported at once.",
      dropLabel: "Drop fina's portfolio_history.csv here, or pick a file.",
      hint: 'Re-running the migration is safe — snapshots already imported are skipped.',
    },
  ];

  let sourceValue = $state(sources[0].value);
  const selected = $derived(sources.find((s) => s.value === sourceValue) ?? sources[0]);
</script>

<div class="import-body">
  <section class="import-card">
    <Field label="Source">
      <Select
        value={sourceValue}
        options={sources.map((s) => ({ value: s.value, label: s.label }))}
        onValueChange={(v) => (sourceValue = v)}
        ariaLabel="Import source"
        fullWidth
      />
    </Field>
    <p class="import-desc">{selected.description}</p>
    <PortfolioUpload source={selected.value}>
      {#snippet prompt()}
        {selected.dropLabel}
        <span class="hint">{selected.hint}</span>
      {/snippet}
    </PortfolioUpload>
  </section>

  <p class="import-footnote">
    Imported snapshots appear on the
    <a href="{base}/money/portfolio/overview">Overview</a> and
    <a href="{base}/money/portfolio/history">History</a> tabs. Account groups and symbol
    classifications are edited in <a href="{base}/money/settings">Money settings</a>.
  </p>
</div>

<style>
  /* Auto margins center the card block in the section body's flex column —
     both axes while it fits, an ordinary scroll once it doesn't. */
  .import-body {
    padding: var(--space-4) var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    width: min(640px, 100%);
    margin: auto;
  }

  .import-card {
    background: var(--surface-card);
    border-radius: var(--radius-card);
    padding: var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .import-desc {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  .import-footnote {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .import-footnote a {
    color: var(--accent-blue);
    text-decoration: none;
  }

  .import-footnote a:hover {
    text-decoration: underline;
  }
</style>
