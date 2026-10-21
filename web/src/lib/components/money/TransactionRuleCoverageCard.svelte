<script lang="ts">
  import { latest } from '@minot/ui/ui/latest';
  import { onMount } from 'svelte';
  import { Badge, EmptyState, Select, Card, LoadState } from '@minot/ui';
  import { getTransactionRuleCoverage, type RuleCoverageValue } from '$lib/money/api';

  // What recent imports actually carried, and where each value went. The list
  // this card exists to shorten is the fallthrough one — the source values no
  // rule covers, which post to `Expenses:Uncategorized:{slug}` — so those are
  // flagged and sorted to the top whatever their count.
  //
  // `posted_account` is what the most recent row *posted to*, not what the
  // rules would answer now: the endpoint reads stored rows, so an edit made
  // since is not reflected here. That is a statement about history and is
  // labelled as one; the card beside this one answers the live question.

  const FIELD_OPTIONS = [
    { value: 'category', label: 'Source categories' },
    { value: 'account', label: 'Source accounts' },
  ];

  // The two fallbacks `import_transactions` writes: the bare account when the
  // source gave no category, and the slugged one when it did. Matched with the
  // separator rather than as a bare prefix, or a user's own
  // `Expenses:UncategorizedTravel` would be reported as a gap for ever.
  const FALLBACK = 'Expenses:Uncategorized';

  let field = $state('category');
  let values: RuleCoverageValue[] = $state([]);
  let untraced = $state(0);
  let loaded = $state(false);
  let loadError = $state('');
  // Switching the field reloads, and the two reads are not equally fast: a
  // slow `category` response landing after a quick `account` one would restore
  // a stale `untraced` count under account values — the cross-contamination
  // the field switch exists to prevent.
  const loads = latest();

  function isFallthrough(row: RuleCoverageValue): boolean {
    const account = row.posted_account ?? '';
    return account === FALLBACK || account.startsWith(`${FALLBACK}:`);
  }

  // Stable within each group: the endpoint already ordered by count, and
  // reordering only lifts the gaps out of a tail they would otherwise sit in.
  const ordered = $derived([
    ...values.filter((v) => isFallthrough(v)),
    ...values.filter((v) => !isFallthrough(v)),
  ]);

  async function load() {
    const current = loads.next();
    try {
      const body = await getTransactionRuleCoverage({ field: field as 'category' | 'account' });
      if (!current()) return;
      values = body.values;
      // Only the category read carries it: it counts rows with no source
      // category, which says nothing about the account column, since a row
      // can carry a category and no account.
      untraced = body.untraced ?? 0;
      loadError = '';
    } catch (e) {
      if (!current()) return;
      loadError = e instanceof Error ? e.message : 'Failed to load import coverage';
    } finally {
      if (current()) loaded = true;
    }
  }

  onMount(load);
</script>

<Card title="Recent imports">
  {#snippet actions()}
    <Select
      bind:value={field}
      options={FIELD_OPTIONS}
      ariaLabel="Coverage field"
      onValueChange={load}
    />
  {/snippet}
  <LoadState scope="card" loading={!loaded} error={loadError}>
    <p class="card-hint caption">
      The distinct values recent imports carried, and the account each one posted to when it was
      last seen. Values falling through to <code>Expenses:Uncategorized</code> come first — that is the
      list a rule is worth writing for. An edit made since a sync is not reflected here; run it through
      the test above instead.
    </p>

    {#if ordered.length === 0}
      <EmptyState>No imports yet.</EmptyState>
    {:else}
      <ul class="divided-list">
        {#each ordered as row (row.value)}
          <li class="cov-row">
            <span class="cov-value code-chip">{row.value}</span>
            <span class="cov-account muted">{row.posted_account ?? '—'}</span>
            {#if isFallthrough(row)}
              <Badge variant="warn">no rule</Badge>
            {/if}
            <span class="cov-count">{row.count}</span>
            <span class="cov-seen muted">{row.last_seen ?? '—'}</span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if untraced > 0}
      <p class="caption untraced">
        {untraced} transactions synced before rule tracing. Their source values were never stored, so
        there is nothing to list — only the count.
      </p>
    {/if}
  </LoadState>
</Card>

<style>
  .card-hint {
    margin: 0 0 var(--space-2);
  }

  .cov-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-1) var(--space-2);
    font-size: var(--text-sm);
  }

  .cov-value {
    color: var(--text-primary);
  }

  .cov-account {
    flex: 1 1 12rem;
    min-width: 0;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }

  .cov-count {
    font-variant-numeric: tabular-nums;
    min-width: 2.5rem;
    text-align: right;
  }

  .cov-seen {
    font-size: var(--text-xs);
    min-width: 5.5rem;
    text-align: right;
  }

  .untraced {
    margin: var(--space-3) 0 0;
  }
</style>
