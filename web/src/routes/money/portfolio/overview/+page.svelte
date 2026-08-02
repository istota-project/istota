<script lang="ts">
  import { base } from '$app/paths';
  import { untrack } from 'svelte';
  import { Chart, DoughnutController, ArcElement, Tooltip, Legend } from 'chart.js';
  import {
    getPortfolioSummary,
    type PortfolioGroupSlice,
    type PortfolioSummary,
  } from '$lib/money/api';
  import { seriesColors } from '$lib/money/portfolioPalette';
  import { chartChrome } from '$lib/chartTheme';
  import { theme } from '$lib/stores/theme';
  import { Button, NoticeBanner, StatTile } from '$lib/components/ui';
  import { directionColor } from '$lib/money/direction';
  import { portfolioGroup, portfolioKnownGroups } from '$lib/money/stores/portfolio';

  Chart.register(DoughnutController, ArcElement, Tooltip, Legend);

  let loading = $state(true);
  let error = $state('');
  let summary = $state<PortfolioSummary | null>(null);
  // The group filter Select lives in the section header (rendered by the
  // portfolio layout); this page feeds it the known groups and reads the
  // chosen value back. The filtered view narrows by_group to one slice, so
  // the option list is pinned from the unfiltered load and only extended,
  // never shrunk.
  $effect(() => {
    if (!$portfolioGroup && summary) {
      portfolioKnownGroups.set(
        (summary.by_group ?? []).map((g) => g.key).filter((k) => k !== 'Ungrouped'),
      );
    }
  });

  const unclassified = $derived(
    (summary?.holdings ?? []).filter((h) => h.asset_class === 'Unclassified').map((h) => h.symbol),
  );

  async function load(forGroup: string) {
    loading = summary === null;
    error = '';
    try {
      const resp = await getPortfolioSummary(forGroup ? { group: forGroup } : undefined);
      summary = resp.summary;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load portfolio';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    const g = $portfolioGroup;
    untrack(() => void load(g));
  });

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

  function pct(value: number | null): string {
    if (value == null) return '—';
    return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(1)}%`;
  }

  function qty(value: number | null): string {
    if (value == null) return '—';
    return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  }

  const totalGain = $derived.by(() => {
    const withBasis = (summary?.holdings ?? []).filter((h) => h.gain != null);
    if (withBasis.length === 0) return null;
    return withBasis.reduce((a, h) => a + (h.gain ?? 0), 0);
  });

  // --- Allocation donuts ---------------------------------------------------

  let classCanvas: HTMLCanvasElement | undefined = $state();
  let typeCanvas: HTMLCanvasElement | undefined = $state();
  let geoCanvas: HTMLCanvasElement | undefined = $state();
  const charts: Chart[] = [];

  function buildDonut(canvas: HTMLCanvasElement, slices: PortfolioGroupSlice[]): Chart {
    const chrome = chartChrome();
    const labels = slices.map((s) => s.key);
    const colors = seriesColors(labels);
    return new Chart(canvas, {
      type: 'doughnut',
      data: {
        labels,
        datasets: [
          {
            data: slices.map((s) => s.value),
            /* design-lint-allow: data viz — Chart.js config can't resolve var();
               the palette lives in $lib/money/portfolioPalette (validated). The
               fallback is unreachable: seriesColors maps every label passed. */
            backgroundColor: labels.map((l) => colors.get(l) ?? '#888888'),
            // 2px surface gap between segments (the spacer rule), on the
            // card surface the donut sits on.
            borderColor: chrome.tooltipBg,
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '62%',
        plugins: {
          legend: {
            position: 'bottom',
            labels: { color: chrome.tick, boxWidth: 10, boxHeight: 10, font: { size: 11 } },
          },
          tooltip: {
            backgroundColor: chrome.tooltipBg,
            borderColor: chrome.tooltipBorder,
            borderWidth: 1,
            titleColor: chrome.tooltipTitle,
            bodyColor: chrome.tooltipBody,
            padding: 10,
            callbacks: {
              label: (ctx) => {
                const slice = slices[ctx.dataIndex];
                return `${slice.key}: ${usd(slice.value)} (${(slice.pct * 100).toFixed(1)}%)`;
              },
            },
          },
        },
      },
    });
  }

  $effect(() => {
    const s = summary;
    // Chart.js holds colors as plain config, so a theme flip needs a rebuild.
    const _theme = $theme;
    if (!s) return;
    untrack(() => {
      while (charts.length) charts.pop()?.destroy();
      if (classCanvas) charts.push(buildDonut(classCanvas, s.by_asset_class));
      if (typeCanvas) charts.push(buildDonut(typeCanvas, s.by_account_type));
      if (geoCanvas) charts.push(buildDonut(geoCanvas, s.by_geography));
    });
  });
</script>

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg">{error}</div>
{:else if !summary}
  <div class="portfolio-empty">
    <p class="empty">
      No portfolio snapshots yet. Import a Fidelity Portfolio Positions CSV to get started.
    </p>
    <div>
      <Button variant="primary" href="{base}/money/portfolio/import">Import a snapshot</Button>
    </div>
  </div>
{:else}
  <div class="money-toolbar">
    <!-- Lone child of a space-between toolbar, so it needs the push right. -->
    <span class="money-result-count as-of">
      As of {summary.exported_at.slice(0, 10)}
      {#if summary.exported_at_estimated}(estimated date){/if}
      · {summary.position_count} positions
    </span>
  </div>

  {#if unclassified.length > 0}
    <div class="money-notice-bar spaced">
      <NoticeBanner variant="info" title="Unclassified symbols">
        {unclassified.join(', ')} — classify them in
        <a href="{base}/money/settings">Money settings</a> to complete the allocation charts.
      </NoticeBanner>
    </div>
  {/if}

  <div class="portfolio-body">
    <div class="summary-cards">
      <StatTile label="Total value" surface align="center">{usd(summary.total_value)}</StatTile>
      <StatTile
        label="Unrealized P&amp;L"
        surface
        align="center"
        valueColor={directionColor(totalGain ?? 0)}
      >
        {usd(totalGain)}
      </StatTile>
      <StatTile label="Holdings" surface align="center">{summary.holdings.length}</StatTile>
      <StatTile label="Accounts" surface align="center">{summary.by_account.length}</StatTile>
    </div>

    <div class="donut-grid">
      <div class="donut-card">
        <div class="micro-label">Asset class</div>
        <div class="donut-canvas"><canvas bind:this={classCanvas}></canvas></div>
      </div>
      <div class="donut-card">
        <div class="micro-label">Account type</div>
        <div class="donut-canvas"><canvas bind:this={typeCanvas}></canvas></div>
      </div>
      <div class="donut-card">
        <div class="micro-label">Geography</div>
        <div class="donut-canvas"><canvas bind:this={geoCanvas}></canvas></div>
      </div>
    </div>

    <div class="micro-label holdings-label">Holdings</div>
    <div class="money-table-header holdings-header">
      <span class="col-symbol">Symbol</span>
      <span class="col-desc">Description</span>
      <span class="col-class">Class</span>
      <span class="money-amount col-qty">Qty</span>
      <span class="money-amount col-value">Value</span>
      <span class="money-amount col-gain">P&amp;L</span>
      <span class="money-amount col-gainpct">P&amp;L %</span>
    </div>
    <div class="holdings-table">
      {#each summary.holdings as holding (holding.symbol)}
        <div class="money-table-row holdings-row">
          <span class="col-symbol symbol">
            {#if holding.symbol === 'CASH'}
              {holding.symbol}
            {:else}
              <a href="{base}/money/portfolio/history?symbol={encodeURIComponent(holding.symbol)}"
                >{holding.symbol}</a
              >
            {/if}
          </span>
          <span class="col-desc desc" title={holding.description}>{holding.description}</span>
          <span class="col-class class-label">
            {holding.asset_class}{holding.sub_class && holding.sub_class !== 'Unclassified'
              ? ` · ${holding.sub_class}`
              : ''}
          </span>
          <span class="money-amount col-qty">{qty(holding.quantity)}</span>
          <span class="money-amount col-value">{usd(holding.value, 2)}</span>
          <span
            class="money-amount col-gain"
            class:positive={(holding.gain ?? 0) > 0}
            class:negative={(holding.gain ?? 0) < 0}
          >
            {usd(holding.gain, 2)}
          </span>
          <span
            class="money-amount col-gainpct"
            class:positive={(holding.gain_pct ?? 0) > 0}
            class:negative={(holding.gain_pct ?? 0) < 0}
          >
            {pct(holding.gain_pct)}
          </span>
        </div>
      {/each}
    </div>
  </div>
{/if}

<style>
  .as-of {
    margin-left: auto;
  }

  .portfolio-empty {
    padding: var(--space-6) var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    max-width: 640px;
  }

  .portfolio-body {
    padding: 0 0 var(--space-4);
  }

  /* This notice follows the toolbar instead of opening the pane, which is the
     case the shell's top padding is for — there it clears the section header
     (work and clients both lead with theirs). Here the toolbar's own bottom
     padding already leaves the gap, so the shell's doubles it; the space
     belongs under the notice, where it would otherwise sit directly on the
     summary cards. */
  .money-notice-bar.spaced {
    padding-top: 0;
    padding-bottom: var(--space-3);
  }

  /* minmax(0, 1fr) everywhere a chart can live: a bare 1fr track's min size is
     the canvas's rendered width, so the grid could grow with the viewport but
     never shrink back. */
  .summary-cards {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: var(--space-3);
    padding: 0 var(--space-3) var(--space-3);
  }

  .donut-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--space-3);
    padding: 0 var(--space-3) var(--space-4);
  }

  .donut-card {
    background: var(--surface-card);
    border-radius: var(--radius-card);
    padding: var(--space-3);
    min-width: 0;
  }

  .donut-canvas {
    height: 220px;
    position: relative;
  }

  .holdings-label {
    padding: 0 var(--space-3) var(--space-1);
  }

  .symbol {
    font-weight: 600;
  }

  .symbol a {
    color: var(--accent-blue);
    text-decoration: none;
  }

  .symbol a:hover {
    text-decoration: underline;
  }

  .col-symbol {
    width: 4.5rem;
    flex-shrink: 0;
  }

  .col-desc {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-muted);
  }

  .col-class {
    width: 13rem;
    flex-shrink: 0;
    color: var(--text-muted);
    font-size: var(--text-xs);
  }

  .col-qty {
    width: 6rem;
  }

  .col-value {
    width: 7.5rem;
  }

  .col-gain {
    width: 7rem;
  }

  .col-gainpct {
    width: 4.5rem;
  }

  @media (max-width: 860px) {
    .donut-grid {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  @media (max-width: 640px) {
    .summary-cards {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .col-class,
    .col-qty,
    .col-gain {
      display: none;
    }

    /* Narrower gaps and columns and smaller type on a phone, so the
       description column gets the width back. Horizontal only: the inline
       padding stays the shell's, which fixes every element on the page to the
       same 0.75rem edge, and the block padding stays the shell's too — this
       table used to tighten it to a hairline and so sat at a different row
       height from work, invoices and transactions on the same phone. */
    .holdings-header,
    .holdings-row {
      gap: var(--space-2);
      font-size: var(--text-xs);
    }

    .holdings-header .col-symbol,
    .holdings-row .col-symbol {
      width: 3.5rem;
    }

    .holdings-header .col-value,
    .holdings-row .col-value {
      width: 5.5rem;
    }

    .holdings-header .col-gainpct,
    .holdings-row .col-gainpct {
      width: 3.5rem;
    }
  }
</style>
