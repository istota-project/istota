<script lang="ts">
  import { goto } from '$app/navigation';
  import { Collapsible } from 'bits-ui';
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
  import { Card, EmptyState, StatTile, PageLoading } from '$lib/components/ui';
  import { getCashFlow, type CashFlowRow } from '$lib/money/api';
  import { directionColor, INCOME_COLOR, EXPENSE_COLOR } from '$lib/money/direction';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { selectedYear, transactionsUrl } from '$lib/money/stores/transactions';
  import { parseAmount, formatAmount } from '$lib/money/utils/accounts';
  import { onDestroy, untrack } from 'svelte';
  import { theme } from '$lib/stores/theme';
  import { chartChrome } from '$lib/chartTheme';

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

  function navigateToAccount(fullName: string) {
    goto(transactionsUrl(fullName, $selectedYear));
  }

  let loading = $state(true);
  let error = $state('');
  let rows: CashFlowRow[] = $state([]);
  let chartCanvas: HTMLCanvasElement | undefined = $state();
  let chart: Chart | undefined;
  let selectedMonthIndex = $state(-1); // -1 = latest month
  let incomeOpen = $state(true);
  let expenseOpen = $state(true);

  interface MonthData {
    label: string;
    year: number;
    month: number;
    income: number;
    expenses: number;
    net: number;
    currency: string;
    incomeByAccount: Map<string, number>;
    expenseByAccount: Map<string, number>;
  }

  let months: MonthData[] = $derived.by(() => {
    const map = new Map<string, MonthData>();

    for (const row of rows) {
      const y = parseInt(row.year);
      const m = parseInt(row.month);
      const key = `${y}-${String(m).padStart(2, '0')}`;
      const pos = row['sum(position)'] || '';
      const amount = parseAmount(pos);
      if (isNaN(amount)) continue;

      let currency = '';
      const cm = pos.match(/[A-Z]{2,}/);
      if (cm) currency = cm[0];

      if (!map.has(key)) {
        const monthNames = [
          'Jan',
          'Feb',
          'Mar',
          'Apr',
          'May',
          'Jun',
          'Jul',
          'Aug',
          'Sep',
          'Oct',
          'Nov',
          'Dec',
        ];
        map.set(key, {
          label: `${monthNames[m - 1]} ${y}`,
          year: y,
          month: m,
          income: 0,
          expenses: 0,
          net: 0,
          currency,
          incomeByAccount: new Map(),
          expenseByAccount: new Map(),
        });
      }

      const md = map.get(key)!;
      if (!md.currency && currency) md.currency = currency;

      if (row.account.startsWith('Income:')) {
        const absAmt = Math.abs(amount);
        md.income += absAmt;
        const existing = md.incomeByAccount.get(row.account) || 0;
        md.incomeByAccount.set(row.account, existing + absAmt);
      } else if (row.account.startsWith('Expenses:')) {
        const absAmt = Math.abs(amount);
        md.expenses += absAmt;
        const existing = md.expenseByAccount.get(row.account) || 0;
        md.expenseByAccount.set(row.account, existing + absAmt);
      }
    }

    const sorted = [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
    return sorted.map(([, md]) => {
      md.net = md.income - md.expenses;
      return md;
    });
  });

  let activeMonth = $derived(
    months.length > 0
      ? months[
          selectedMonthIndex >= 0 && selectedMonthIndex < months.length
            ? selectedMonthIndex
            : months.length - 1
        ]
      : null,
  );

  let savingsRate = $derived(
    activeMonth && activeMonth.income > 0
      ? Math.round((activeMonth.net / activeMonth.income) * 100)
      : 0,
  );

  let sortedIncome = $derived(
    activeMonth ? [...activeMonth.incomeByAccount.entries()].sort((a, b) => b[1] - a[1]) : [],
  );

  let sortedExpenses = $derived(
    activeMonth ? [...activeMonth.expenseByAccount.entries()].sort((a, b) => b[1] - a[1]) : [],
  );

  function shortAccountName(account: string): string {
    const parts = account.split(':');
    return parts.slice(1).join(':');
  }

  function pctOfTotal(amount: number, total: number): string {
    if (total === 0) return '0%';
    return `${((amount / total) * 100).toFixed(1)}%`;
  }

  async function loadReport(ledger: string | undefined, year: number | undefined) {
    loading = true;
    error = '';
    try {
      const resp = await getCashFlow({ ledger, year });
      rows = resp.results;
      selectedMonthIndex = -1;
    } catch (e) {
      if (e instanceof Error) error = e.message;
      else error = 'Failed to load report';
    } finally {
      loading = false;
    }
  }

  function buildChart() {
    if (!chartCanvas || months.length === 0) return;

    if (chart) chart.destroy();

    const labels = months.map((m) => m.label);
    const incomeData = months.map((m) => m.income);
    const expenseData = months.map((m) => -m.expenses);
    const netData = months.map((m) => m.net);

    const chrome = chartChrome();
    chart = new Chart(chartCanvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            type: 'bar',
            label: 'Income',
            data: incomeData,
            /* design-lint-allow-begin: data viz — Chart.js takes a config
               object and never reads the cascade, so it cannot resolve var().
               Chart chrome is themed by $lib/chartTheme; these are series. */
            backgroundColor: 'rgba(74, 219, 192, 0.35)',
            borderColor: 'rgba(74, 219, 192, 0.6)',
            borderWidth: 1,
            borderRadius: 2,
            stack: 'main',
            order: 2,
          },
          {
            type: 'bar',
            label: 'Expenses',
            data: expenseData,
            backgroundColor: 'rgba(212, 106, 181, 0.35)',
            borderColor: 'rgba(212, 106, 181, 0.6)',
            /* design-lint-allow-end */
            borderWidth: 1,
            borderRadius: 2,
            stack: 'main',
            order: 2,
          },
          {
            type: 'line',
            label: 'Net',
            data: netData,
            borderColor: chrome.neutral,
            backgroundColor: 'transparent',
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: chrome.neutral,
            tension: 0.3,
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: 'index',
          intersect: false,
        },
        onClick: (_event, elements) => {
          if (elements.length > 0) {
            selectedMonthIndex = elements[0].index;
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: chrome.tooltipBg,
            borderColor: chrome.tooltipBorder,
            borderWidth: 1,
            titleColor: chrome.tooltipTitle,
            bodyColor: chrome.tooltipBody,
            padding: 10,
            callbacks: {
              label: (ctx) => {
                const val = ctx.parsed.y ?? 0;
                const currency = months[0]?.currency || '';
                return `${ctx.dataset.label}: ${formatAmount(val, currency)}`;
              },
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: {
              color: chrome.tick,
              font: { size: 11 },
            },
            border: { display: false },
          },
          y: {
            grid: {
              color: chrome.grid,
            },
            ticks: {
              color: chrome.tick,
              font: { size: 11 },
              callback: (value) => {
                const num = Number(value);
                if (Math.abs(num) >= 1000) return `$${(num / 1000).toFixed(0)}K`;
                return `$${num}`;
              },
            },
            border: { display: false },
          },
        },
      },
    });
  }

  $effect(() => {
    const ledger = $selectedLedger || undefined;
    const year = $selectedYear || undefined;
    untrack(() => loadReport(ledger, year));
  });

  $effect(() => {
    const _m = months;
    const _loading = loading;
    // Chart.js holds its colors as plain config, so a theme flip needs a
    // rebuild — reading $theme here makes this effect depend on it.
    const _theme = $theme;
    if (!_loading && chartCanvas && _m.length > 0) {
      untrack(() => buildChart());
    }
  });
  onDestroy(() => {
    chart?.destroy();
    chart = undefined;
  });
</script>

{#if loading}
  <PageLoading />
{:else if error}
  <div class="center-msg error">{error}</div>
{:else if months.length === 0}
  <EmptyState>No data for the selected period.</EmptyState>
{:else}
  <div class="cashflow-page">
    <Card as="div" variant="flat" padding="sm" class="chart-container">
      <canvas bind:this={chartCanvas}></canvas>
    </Card>

    {#if activeMonth}
      <div class="month-title">
        {activeMonth.label}
      </div>

      <div class="summary-cards">
        <StatTile label="Income" surface align="center" valueColor={INCOME_COLOR}>
          {formatAmount(activeMonth.income, activeMonth.currency)}
        </StatTile>
        <StatTile label="Expenses" surface align="center" valueColor={EXPENSE_COLOR}>
          {formatAmount(activeMonth.expenses, activeMonth.currency)}
        </StatTile>
        <!-- `>= 0` rather than `> 0`: a net of exactly zero reads as the
             break-even it is, which is the income side of the line here. -->
        <StatTile
          label="Net income"
          surface
          align="center"
          valueColor={activeMonth.net >= 0 ? INCOME_COLOR : EXPENSE_COLOR}
        >
          {formatAmount(activeMonth.net, activeMonth.currency)}
        </StatTile>
        <StatTile label="Margin" surface align="center" valueColor={directionColor(savingsRate)}>
          {savingsRate}%
        </StatTile>
      </div>

      <div class="breakdowns">
        <Collapsible.Root bind:open={incomeOpen}>
          <div class="section-header">
            <Collapsible.Trigger class="section-toggle button-reset">
              <span class="record-caret" class:is-open={incomeOpen}>&#9654;</span>
              Income
            </Collapsible.Trigger>
          </div>
          <Collapsible.Content>
            <div class="breakdown-list">
              {#each sortedIncome as [account, amount]}
                <div class="record-row record-row--tree">
                  <button
                    class="record-name button-reset"
                    type="button"
                    onclick={() => navigateToAccount(account)}
                  >
                    {shortAccountName(account)}
                  </button>
                  <span class="breakdown-amount num income">
                    {formatAmount(amount, activeMonth.currency)} ({pctOfTotal(
                      amount,
                      activeMonth.income,
                    )})
                  </span>
                </div>
              {/each}
            </div>
          </Collapsible.Content>
        </Collapsible.Root>

        <Collapsible.Root bind:open={expenseOpen}>
          <div class="section-header">
            <Collapsible.Trigger class="section-toggle button-reset">
              <span class="record-caret" class:is-open={expenseOpen}>&#9654;</span>
              Expenses
            </Collapsible.Trigger>
          </div>
          <Collapsible.Content>
            <div class="breakdown-list">
              {#each sortedExpenses as [account, amount]}
                <div class="record-row record-row--tree">
                  <button
                    class="record-name button-reset"
                    type="button"
                    onclick={() => navigateToAccount(account)}
                  >
                    {shortAccountName(account)}
                  </button>
                  <span class="breakdown-amount num expense">
                    {formatAmount(amount, activeMonth.currency)} ({pctOfTotal(
                      amount,
                      activeMonth.expenses,
                    )})
                  </span>
                </div>
              {/each}
            </div>
          </Collapsible.Content>
        </Collapsible.Root>
      </div>
    {/if}
  </div>
{/if}

<style>
  /* Block rather than the card's flex column: Chart.js sizes the canvas
     from this box. */
  .cashflow-page :global(.chart-container) {
    display: block;
    height: 280px;
    margin-bottom: var(--space-4);
  }

  .month-title {
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    padding: var(--space-2) var(--space-3);
  }

  /* Block padding only. An inline inset here would sit *inside* the frame's,
     putting the cards a step in from the chart card above them — the whole
     page reads as one column of cards, and the report convention is that a
     card's box sits on the frame edge while only text steps in (the
     .section-header below, balance-sheet's net-worth banner). */
  .summary-cards {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: var(--space-3);
    padding-block: var(--space-2);
    margin-bottom: var(--space-3);
  }

  /* The summary tiles are `StatTile`; the breakdown rows below still carry
     their own direction colours, since they are table cells rather than
     tiles. Both read the same mapping — see lib/money/direction.ts. */
  .breakdown-amount.income {
    color: var(--money-income);
  }
  .breakdown-amount.expense {
    color: var(--money-expense);
  }

  /* `.breakdowns` is a bare grouping element: its `.section-header` rows carry
     their own inline padding from the report shell, exactly as income-statement's
     do, so an inset here only pushed them a step deeper than the same rows on
     the sibling reports. */

  .breakdown-list {
    padding: var(--space-1) 0 var(--space-2);
  }

  /* The per-account rows are `.record-row--tree` from table.css, the same
     shell the balance-sheet and income-statement trees use. They had been a
     private copy of it that differed in exactly one value — a 0.25rem block
     padding against the tree's 0.2rem — so the three reports sat at two row
     heights. Only the columns are styled here. */
  .breakdown-amount {
    margin-left: auto;
  }

  @media (max-width: 640px) {
    .summary-cards {
      grid-template-columns: repeat(2, 1fr);
    }

    .cashflow-page :global(.chart-container) {
      height: 200px;
    }
  }
</style>
