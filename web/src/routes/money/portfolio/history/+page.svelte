<script lang="ts">
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
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
  import { Badge, Button, ConfirmDialog, KebabMenu, Modal } from '$lib/components/ui';
  import { notifyError, notifySuccess } from '$lib/stores/notices';
  import { portfolioGroupBy, type PortfolioGroupBy } from '$lib/money/stores/portfolio';

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

  let loading = $state(true);
  let error = $state('');
  let series: PortfolioHistoryPoint[] = $state([]);
  let snapshots: PortfolioSnapshotRow[] = $state([]);
  // The group-by Select lives in the section header (portfolio layout),
  // shared via the store.
  const groupBy = $derived($portfolioGroupBy);
  // Deep link from a holdings row: /history?symbol=VTI charts that symbol.
  // URL-derived (not copied into state) so the layout sees the same value
  // and can drop the group-by control while a symbol is charted.
  const symbol = $derived(page.url.searchParams.get('symbol') ?? '');
  let symbolPoints: { exported_at: string; value: number | null; quantity: number | null }[] =
    $state([]);

  let chartCanvas: HTMLCanvasElement | undefined = $state();
  let chart: Chart | undefined;

  let confirmDeleteId: number | null = $state(null);
  let diff: PortfolioDiff | null = $state(null);
  let diffOpen = $state(false);

  function qty(value: number | null): string {
    if (value == null) return '—';
    return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  }

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

  async function load(currentGroupBy: PortfolioGroupBy, currentSymbol: string) {
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

  const diffEmpty = $derived.by(() => {
    if (!diff) return true;
    return diff.opened.length === 0 && diff.closed.length === 0 && diff.changed.length === 0;
  });

  // Which two snapshots the dialog is showing. The diff carries only ids, and a
  // full-screen dialog that doesn't say what it is comparing is a wall of rows.
  const diffSubtitle = $derived.by(() => {
    if (!diff) return '';
    const stamp = (id: number) => {
      const snap = snapshots.find((s) => s.id === id);
      return snap ? snap.exported_at.slice(0, 16).replace('T', ' ') : `#${id}`;
    };
    return `${stamp(diff.older_id)} → ${stamp(diff.newer_id)}`;
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
        <button
          type="button"
          class="clear-symbol"
          onclick={() => goto(`${base}/money/portfolio/history`)}
        >
          ← All holdings
        </button>
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
    <!-- Header inside the .money-table scroller (the work/invoices shape), so
         the labels travel with the columns they name if the row has to scroll,
         and the section fits the viewport instead of pushing the page wide. -->
    <div class="money-table">
      <div class="money-table-header">
        <span class="col-date">Exported</span>
        <span class="col-source">Source file</span>
        <span class="money-amount col-positions">Positions</span>
        <span class="money-amount col-total">Total value</span>
        <span class="money-kebab-spacer"></span>
      </div>
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

<!-- A diff is a list, not a form: it takes the screen it can get, and the
     money record-table shell so its rows read like every other money table.
     The Modal's own max-width/max-height cap these to the safe box. -->
<Modal
  open={diffOpen}
  title="Changes since previous snapshot"
  description={diffSubtitle}
  onOpenChange={(open) => {
    if (!open) diffOpen = false;
  }}
  width="100vw"
  height="100dvh"
>
  {#if diff}
    {#if diffEmpty}
      <p class="empty">No position changes between these snapshots.</p>
    {:else}
      {#each [{ key: 'opened', label: 'Opened', rows: diff.opened }, { key: 'closed', label: 'Closed', rows: diff.closed }] as section (section.key)}
        {#if section.rows.length > 0}
          <section class="diff-section">
            <div class="micro-label diff-heading">
              {section.label}
              <span class="diff-count">{section.rows.length}</span>
            </div>
            <div class="money-table diff-table">
              <div class="money-table-header">
                <span class="diff-col-symbol">Symbol</span>
                <span class="diff-col-account">Account</span>
                <span class="money-amount diff-col-qty">Qty</span>
                <span class="money-amount diff-col-value">Value</span>
              </div>
              {#each section.rows as entry (entry.account_name + entry.symbol)}
                <div class="money-table-row">
                  <span class="diff-col-symbol diff-symbol">{entry.symbol}</span>
                  <span class="diff-col-account">{entry.account_name}</span>
                  <span class="money-amount diff-col-qty">{qty(entry.quantity)}</span>
                  <span class="money-amount diff-col-value">{usd(entry.value, 2)}</span>
                </div>
              {/each}
            </div>
          </section>
        {/if}
      {/each}
      {#if diff.changed.length > 0}
        <section class="diff-section">
          <div class="micro-label diff-heading">
            Changed
            <span class="diff-count">{diff.changed.length}</span>
          </div>
          <div class="money-table diff-table">
            <div class="money-table-header">
              <span class="diff-col-symbol">Symbol</span>
              <span class="diff-col-account">Account</span>
              <span class="money-amount diff-col-qty">Qty</span>
              <span class="money-amount diff-col-value">Value</span>
              <span class="money-amount diff-col-change">Change</span>
            </div>
            {#each diff.changed as entry (entry.account_name + entry.symbol)}
              {@const delta = entry.value_to - entry.value_from}
              <div class="money-table-row">
                <span class="diff-col-symbol diff-symbol">{entry.symbol}</span>
                <span class="diff-col-account">{entry.account_name}</span>
                <span class="money-amount diff-col-qty">
                  {#if entry.quantity_from === entry.quantity_to}
                    {qty(entry.quantity_to)}
                  {:else}
                    <span class="diff-was">{qty(entry.quantity_from)}</span>
                    <span class="diff-arrow">→</span>
                    {qty(entry.quantity_to)}
                  {/if}
                </span>
                <span class="money-amount diff-col-value">
                  <span class="diff-was">{usd(entry.value_from, 2)}</span>
                  <span class="diff-arrow">→</span>
                  {usd(entry.value_to, 2)}
                </span>
                <span
                  class="money-amount diff-col-change"
                  class:positive={delta > 0}
                  class:negative={delta < 0}
                >
                  {delta > 0 ? '+' : ''}{usd(delta, 2)}
                </span>
              </div>
            {/each}
          </div>
        </section>
      {/if}
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

  .diff-section {
    margin-bottom: var(--space-4);
  }

  .diff-section:last-child {
    margin-bottom: 0;
  }

  /* The shell's rows carry their own inline padding, which inside the dialog's
     own padding would indent every column a notch past the title. They drop it
     and sit on the dialog's edge instead: the row's hover fill is
     --surface-card, which is the panel's own colour in here, so there is no
     fill for the inset to protect — and pulling the table out with a negative
     margin instead left the body 0.75rem horizontally scrollable. */
  .diff-table .money-table-header,
  .diff-table .money-table-row {
    padding-inline: 0;
  }

  .diff-heading {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    padding: 0 0 var(--space-1);
  }

  .diff-count {
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
  }

  .diff-symbol {
    font-weight: 600;
  }

  .diff-col-symbol {
    width: 5rem;
    flex-shrink: 0;
  }

  .diff-col-account {
    flex: 1;
    min-width: 0;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .diff-col-qty {
    width: 10rem;
  }

  .diff-col-value {
    width: 14rem;
  }

  .diff-col-change {
    width: 8rem;
  }

  /* The previous value, kept quiet: the row is about what it became. */
  .diff-was,
  .diff-arrow {
    color: var(--text-dim);
  }

  .positive {
    color: var(--money-income);
  }

  .negative {
    color: var(--money-expense);
  }

  @media (max-width: 640px) {
    .col-source {
      display: none;
    }

    .chart-container {
      height: 200px;
    }

    /* Narrower gaps and type on a phone, so the snapshot columns fit a 390px
       screen rather than leaving half the row — Total value included — behind
       the fold. Horizontal only: the padding is deliberately left alone in
       both axes. The shell fixes every element on the page to the same 0.75rem
       inline edge, and it owns the row rhythm — this table used to tighten the
       block padding to a hairline, which put it at a different row height from
       every non-portfolio money table on the same phone. */
    .money-table-header,
    .money-table-row {
      gap: var(--space-2);
      font-size: var(--text-xs);
    }

    /* The source column is what absorbs the leftover width on the wide layout,
       and it is hidden here, so the date column takes that job over: the row
       still spans the table and the kebab still sits on its right edge, rather
       than the row shrinking to its content and stranding the actions
       mid-table. */
    .col-date {
      flex: 1;
      min-width: 0;
      /* Date and time fit; the rare "est." badge wraps under them rather than
         overflowing a fixed-width column onto the count beside it. */
      flex-wrap: wrap;
    }

    .col-positions {
      width: 3.5rem;
    }

    .col-total {
      width: 6rem;
    }

    /* The diff dialog is the width of the phone, so the cells that exist to
       show a transition give that up: the previous value and its arrow go, the
       cell keeps the value it became, and the Change column still says by how
       much. Qty goes with them — value and delta are the reading. */
    .diff-was,
    .diff-arrow,
    .diff-col-qty {
      display: none;
    }

    /* Four columns do not fit a phone, and the account is not the one to drop:
       a snapshot holds the same symbol in several accounts, so without it two
       VTI rows are indistinguishable. It wraps to its own line under the
       symbol instead, and the symbol takes over as the flexible column so the
       numbers still land on the table's right edge. */
    .diff-table .money-table-row {
      flex-wrap: wrap;
      row-gap: 0;
    }

    .diff-col-symbol {
      flex: 1;
      width: auto;
    }

    .diff-col-account {
      flex: 1 1 100%;
      order: 9;
      font-size: var(--text-2xs);
      color: var(--text-dim);
    }

    /* No longer a column of its own, so it has no label — the symbol column
       label stands for the pair. */
    .diff-table .money-table-header .diff-col-account {
      display: none;
    }

    .diff-col-value {
      width: 6rem;
    }

    .diff-col-change {
      width: 5.5rem;
    }
  }
</style>
