<script lang="ts">
  import { page } from '$app/state';
  import { untrack } from 'svelte';
  import {
    Chart,
    BarController,
    BarElement,
    LineController,
    LineElement,
    PointElement,
    CategoryScale,
    LinearScale,
    Tooltip,
    Legend,
  } from 'chart.js';
  import {
    deletePortfolioSnapshot,
    getPortfolioDiff,
    getPortfolioHistory,
    getPortfolioSnapshots,
    getPortfolioSymbolHistory,
    type PortfolioDiff,
    type PortfolioHistoryPoint,
    type PortfolioSnapshotRow,
  } from '$lib/money/api';
  import { seriesColors } from '$lib/money/portfolioPalette';
  import { chartChrome } from '$lib/chartTheme';
  import { theme } from '$lib/stores/theme';
  import { base } from '$app/paths';
  import { Badge, Button, ConfirmDialog, KebabMenu, Modal, Select } from '$lib/components/ui';
  import { notifyError, notifySuccess } from '$lib/stores/notices';

  Chart.register(
    BarController,
    BarElement,
    LineController,
    LineElement,
    PointElement,
    CategoryScale,
    LinearScale,
    Tooltip,
    Legend,
  );

  type GroupBy = 'total' | 'owner' | 'account_type' | 'asset_class';

  let loading = $state(true);
  let error = $state('');
  let series: PortfolioHistoryPoint[] = $state([]);
  let snapshots: PortfolioSnapshotRow[] = $state([]);
  let groupBy: GroupBy = $state('total');
  // Deep link from a holdings row: /history?symbol=VTI charts that symbol.
  let symbol = $state(page.url.searchParams.get('symbol') ?? '');
  let symbolPoints: { exported_at: string; value: number | null; quantity: number | null }[] =
    $state([]);

  let chartCanvas: HTMLCanvasElement | undefined = $state();
  let chart: Chart | undefined;

  let confirmDeleteId: number | null = $state(null);
  let diff: PortfolioDiff | null = $state(null);
  let diffOpen = $state(false);

  const groupOptions = [
    { value: 'total', label: 'Total' },
    { value: 'owner', label: 'By owner' },
    { value: 'account_type', label: 'By account type' },
    { value: 'asset_class', label: 'By asset class' },
  ];

  function usd(value: number | null, fractionDigits = 0): string {
    if (value == null) return '—';
    const sign = value < 0 ? '-' : '';
    return (
      sign +
      '$' +
      Math.abs(value).toLocaleString(undefined, {
        minimumFractionDigits: fractionDigits,
        maximumFractionDigits: fractionDigits,
      })
    );
  }

  async function load(currentGroupBy: GroupBy, currentSymbol: string) {
    error = '';
    try {
      if (currentSymbol) {
        const resp = await getPortfolioSymbolHistory(currentSymbol);
        symbolPoints = resp.history.points;
      } else {
        const resp = await getPortfolioHistory(
          currentGroupBy === 'total' ? undefined : { groupBy: currentGroupBy },
        );
        series = resp.series;
      }
      const snaps = await getPortfolioSnapshots();
      snapshots = snaps.snapshots;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load history';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    const g = groupBy;
    const s = symbol;
    untrack(() => void load(g, s));
  });

  function buildChart() {
    if (!chartCanvas) return;
    if (chart) chart.destroy();
    const chrome = chartChrome();

    const commonOptions = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index' as const, intersect: false },
      plugins: {
        tooltip: {
          backgroundColor: chrome.tooltipBg,
          borderColor: chrome.tooltipBorder,
          borderWidth: 1,
          titleColor: chrome.tooltipTitle,
          bodyColor: chrome.tooltipBody,
          padding: 10,
          callbacks: {
            label: (ctx: { dataset: { label?: string }; parsed: { y: number | null } }) =>
              `${ctx.dataset.label}: ${usd(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: {
          stacked: true,
          grid: { display: false },
          ticks: { color: chrome.tick, font: { size: 11 } },
          border: { display: false },
        },
        y: {
          stacked: true,
          grid: { color: chrome.grid },
          ticks: {
            color: chrome.tick,
            font: { size: 11 },
            callback: (value: string | number) => {
              const num = Number(value);
              if (Math.abs(num) >= 1000) return `$${(num / 1000).toFixed(0)}K`;
              return `$${num}`;
            },
          },
          border: { display: false },
        },
      },
    };

    if (symbol) {
      const labels = symbolPoints.map((p) => p.exported_at.slice(0, 10));
      const colors = seriesColors([symbol]);
      chart = new Chart(chartCanvas, {
        type: 'line',
        data: {
          labels,
          datasets: [
            {
              label: symbol,
              data: symbolPoints.map((p) => p.value),
              borderColor: colors.get(symbol),
              backgroundColor: 'transparent',
              borderWidth: 2,
              pointRadius: 2,
              pointHoverRadius: 4,
              tension: 0.2,
            },
          ],
        },
        options: {
          ...commonOptions,
          plugins: { ...commonOptions.plugins, legend: { display: false } },
        },
      });
      return;
    }

    const labels = series.map((p) => p.exported_at.slice(0, 10));
    if (groupBy === 'total') {
      chart = new Chart(chartCanvas, {
        type: 'line',
        data: {
          labels,
          datasets: [
            {
              label: 'Total value',
              data: series.map((p) => p.total),
              borderColor: chrome.neutral,
              backgroundColor: 'transparent',
              borderWidth: 2,
              pointRadius: 2,
              pointHoverRadius: 4,
              tension: 0.2,
            },
          ],
        },
        // One series: the title names it, so no legend box (dataviz rule).
        options: {
          ...commonOptions,
          plugins: { ...commonOptions.plugins, legend: { display: false } },
        },
      });
      return;
    }

    // Grouped: stacked bars, one dataset per label in fixed slot order.
    const groupLabels = [...new Set(series.flatMap((p) => Object.keys(p.groups ?? {})))];
    const colors = seriesColors(groupLabels);
    chart = new Chart(chartCanvas, {
      type: 'bar',
      data: {
        labels,
        datasets: groupLabels.map((label) => ({
          label,
          data: series.map((p) => p.groups?.[label] ?? 0),
          backgroundColor: colors.get(label),
          // 2px surface gap between stacked segments (spacer rule).
          borderColor: chrome.tooltipBg,
          borderWidth: 1,
          borderRadius: 2,
          stack: 'main',
        })),
      },
      options: {
        ...commonOptions,
        plugins: {
          ...commonOptions.plugins,
          legend: {
            position: 'bottom' as const,
            labels: { color: chrome.tick, boxWidth: 10, boxHeight: 10, font: { size: 11 } },
          },
        },
      },
    });
  }

  $effect(() => {
    const _series = series;
    const _points = symbolPoints;
    const _loading = loading;
    // Chart.js holds its colors as plain config, so a theme flip needs a rebuild.
    const _theme = $theme;
    if (!_loading && chartCanvas) {
      untrack(() => buildChart());
    }
  });

  async function handleDelete() {
    const id = confirmDeleteId;
    confirmDeleteId = null;
    if (id == null) return;
    try {
      await deletePortfolioSnapshot(id);
      notifySuccess(`Deleted snapshot #${id}`, { key: 'portfolio:snapshot-delete' });
      await load(groupBy, symbol);
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Delete failed', {
        key: 'portfolio:snapshot-delete',
      });
    }
  }

  async function compareWithPrevious(snapshot: PortfolioSnapshotRow) {
    // snapshots is newest-first; "previous" is the next row down.
    const idx = snapshots.findIndex((s) => s.id === snapshot.id);
    const previous = snapshots[idx + 1];
    if (!previous) return;
    try {
      const resp = await getPortfolioDiff(previous.id, snapshot.id);
      diff = resp.diff;
      diffOpen = true;
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Diff failed', { key: 'portfolio:diff' });
    }
  }

  function snapshotMenu(snapshot: PortfolioSnapshotRow) {
    const idx = snapshots.findIndex((s) => s.id === snapshot.id);
    return [
      {
        label: 'Compare with previous',
        onSelect: () => void compareWithPrevious(snapshot),
        disabled: idx === snapshots.length - 1,
      },
      {
        label: 'Delete',
        danger: true,
        onSelect: () => (confirmDeleteId = snapshot.id),
      },
    ];
  }

  const confirmMessage = $derived.by(() => {
    const snap = snapshots.find((s) => s.id === confirmDeleteId);
    if (!snap) return '';
    return `Are you sure you want to delete the snapshot from ${snap.exported_at.slice(0, 10)} (${snap.position_count} positions)? This permanently removes it and its positions.`;
  });
</script>

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg">{error}</div>
{:else if snapshots.length === 0}
  <div class="portfolio-empty">
    <p class="empty">No snapshots yet — import a positions CSV to start the history.</p>
    <div>
      <Button variant="primary" href="{base}/money/portfolio/import">Import a snapshot</Button>
    </div>
  </div>
{:else}
  <div class="money-toolbar">
    <div class="toolbar-left control-row">
      {#if symbol}
        <span class="symbol-title">{symbol}</span>
        <button type="button" class="clear-symbol" onclick={() => (symbol = '')}>
          ← All holdings
        </button>
      {:else}
        <Select
          value={groupBy}
          options={groupOptions}
          onValueChange={(v) => (groupBy = v as GroupBy)}
          ariaLabel="Group history by"
        />
      {/if}
    </div>
    <span class="money-result-count">
      {snapshots.length} snapshot{snapshots.length === 1 ? '' : 's'}
    </span>
  </div>

  <div class="history-body">
    <div class="chart-container">
      <div class="chart-canvas"><canvas bind:this={chartCanvas}></canvas></div>
    </div>

    <div class="micro-label snapshots-label">Snapshots</div>
    <div class="money-table-header">
      <span class="col-date">Exported</span>
      <span class="col-source">Source file</span>
      <span class="money-amount col-positions">Positions</span>
      <span class="money-amount col-total">Total value</span>
      <span class="money-kebab-spacer"></span>
    </div>
    <div class="snapshot-table">
      {#each snapshots as snapshot (snapshot.id)}
        <div class="money-table-row">
          <span class="col-date">
            {snapshot.exported_at.slice(0, 16).replace('T', ' ')}
            {#if snapshot.exported_at_estimated}
              <Badge variant="warn">est.</Badge>
            {/if}
          </span>
          <span class="col-source" title={snapshot.source_file ?? snapshot.source}>
            {snapshot.source_file ?? snapshot.source}
          </span>
          <span class="money-amount col-positions">{snapshot.position_count}</span>
          <span class="money-amount col-total">{usd(snapshot.total_value, 2)}</span>
          <KebabMenu items={snapshotMenu(snapshot)} ariaLabel="Snapshot actions" />
        </div>
      {/each}
    </div>
  </div>
{/if}

<ConfirmDialog
  open={confirmDeleteId !== null}
  title="Delete snapshot"
  message={confirmMessage}
  confirmLabel="Delete"
  confirmVariant="danger"
  onConfirm={handleDelete}
  onCancel={() => (confirmDeleteId = null)}
/>

<Modal
  open={diffOpen}
  title="Changes since previous snapshot"
  onOpenChange={(open) => {
    if (!open) diffOpen = false;
  }}
  width="520px"
>
  {#if diff}
    {#if diff.opened.length === 0 && diff.closed.length === 0 && diff.changed.length === 0}
      <p class="empty small">No position changes between these snapshots.</p>
    {/if}
    {#if diff.opened.length > 0}
      <div class="micro-label">Opened</div>
      <ul class="diff-list">
        {#each diff.opened as entry (entry.account_name + entry.symbol)}
          <li>
            <span class="diff-symbol">{entry.symbol}</span>
            <span class="diff-account">{entry.account_name}</span>
            <span class="diff-amount">{usd(entry.value, 2)}</span>
          </li>
        {/each}
      </ul>
    {/if}
    {#if diff.closed.length > 0}
      <div class="micro-label">Closed</div>
      <ul class="diff-list">
        {#each diff.closed as entry (entry.account_name + entry.symbol)}
          <li>
            <span class="diff-symbol">{entry.symbol}</span>
            <span class="diff-account">{entry.account_name}</span>
            <span class="diff-amount">{usd(entry.value, 2)}</span>
          </li>
        {/each}
      </ul>
    {/if}
    {#if diff.changed.length > 0}
      <div class="micro-label">Changed</div>
      <ul class="diff-list">
        {#each diff.changed as entry (entry.account_name + entry.symbol)}
          <li>
            <span class="diff-symbol">{entry.symbol}</span>
            <span class="diff-account">{entry.account_name}</span>
            <span class="diff-amount">
              {usd(entry.value_from, 2)} → {usd(entry.value_to, 2)}
              {#if entry.quantity_from !== entry.quantity_to}
                ({entry.quantity_from} → {entry.quantity_to})
              {/if}
            </span>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</Modal>

<style>
  .portfolio-empty {
    padding: var(--space-6) var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    max-width: 640px;
  }

  .toolbar-left {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .symbol-title {
    font-weight: 600;
  }

  .clear-symbol {
    background: none;
    border: none;
    color: var(--accent-blue);
    font: inherit;
    font-size: var(--text-sm);
    cursor: pointer;
    padding: 0;
  }

  .history-body {
    padding: 0 0 var(--space-4);
  }

  .chart-container {
    height: 280px;
    padding: var(--space-3);
    background: var(--surface-card);
    border-radius: var(--radius-card);
    margin: 0 var(--space-3) var(--space-4);
    min-width: 0;
  }

  /* Chart.js sizes the canvas from its parent, and needs that parent
     relatively positioned and dedicated to the canvas to follow a viewport
     shrink — without it the chart grows but never comes back down. */
  .chart-canvas {
    position: relative;
    height: 100%;
    min-width: 0;
  }

  .snapshots-label {
    padding: 0 var(--space-3) var(--space-1);
  }

  .col-date {
    width: 12rem;
    flex-shrink: 0;
    display: inline-flex;
    align-items: baseline;
    gap: var(--space-2);
  }

  .col-source {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-muted);
  }

  .col-positions {
    width: 5.5rem;
  }

  .col-total {
    width: 8rem;
  }

  .diff-list {
    list-style: none;
    margin: 0 0 var(--space-3);
    padding: 0;
    font-size: var(--text-sm);
  }

  .diff-list li {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    /* design-lint-allow: sub---space-1 hairline row rhythm, off the ramp on
       purpose (same figure the money tree rows use) */
    padding: 0.15rem 0;
  }

  .diff-symbol {
    font-weight: 600;
    width: 4.5rem;
    flex-shrink: 0;
  }

  .diff-account {
    flex: 1;
    min-width: 0;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .diff-amount {
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  @media (max-width: 640px) {
    .col-source {
      display: none;
    }

    .chart-container {
      height: 200px;
    }
  }
</style>
