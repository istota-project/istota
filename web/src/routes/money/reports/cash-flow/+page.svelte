<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { Collapsible } from 'bits-ui';
	import { Chart, BarController, BarElement, LineController, LineElement, PointElement, CategoryScale, LinearScale, Tooltip, Legend } from 'chart.js';
	import { getCashFlow, type CashFlowRow } from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';
	import { selectedYear, selectedAccount } from '$lib/money/stores/transactions';
	import { parseAmount, formatAmount } from '$lib/money/utils/accounts';
	import { untrack } from 'svelte';

	Chart.register(BarController, BarElement, LineController, LineElement, PointElement, CategoryScale, LinearScale, Tooltip, Legend);

	function navigateToAccount(fullName: string) {
		selectedAccount.set(fullName);
		goto(`${base}/money/transactions`);
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
				const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
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
			? months[selectedMonthIndex >= 0 && selectedMonthIndex < months.length ? selectedMonthIndex : months.length - 1]
			: null
	);

	let savingsRate = $derived(
		activeMonth && activeMonth.income > 0
			? Math.round((activeMonth.net / activeMonth.income) * 100)
			: 0
	);

	let sortedIncome = $derived(
		activeMonth
			? [...activeMonth.incomeByAccount.entries()].sort((a, b) => b[1] - a[1])
			: []
	);

	let sortedExpenses = $derived(
		activeMonth
			? [...activeMonth.expenseByAccount.entries()].sort((a, b) => b[1] - a[1])
			: []
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

		const labels = months.map(m => m.label);
		const incomeData = months.map(m => m.income);
		const expenseData = months.map(m => -m.expenses);
		const netData = months.map(m => m.net);

		chart = new Chart(chartCanvas, {
			type: 'bar',
			data: {
				labels,
				datasets: [
					{
						type: 'bar',
						label: 'Income',
						data: incomeData,
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
						borderWidth: 1,
						borderRadius: 2,
						stack: 'main',
						order: 2,
					},
					{
						type: 'line',
						label: 'Net',
						data: netData,
						borderColor: '#e0e0e0',
						backgroundColor: 'transparent',
						borderWidth: 2,
						pointRadius: 0,
						pointHoverRadius: 4,
						pointHoverBackgroundColor: '#e0e0e0',
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
						backgroundColor: '#1a1a1a',
						borderColor: '#333',
						borderWidth: 1,
						titleColor: '#e0e0e0',
						bodyColor: '#bbb',
						padding: 10,
						callbacks: {
							label: (ctx) => {
								const val = ctx.parsed.y;
								const abs = Math.abs(val);
								const formatted = abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
								const currency = months[0]?.currency || '';
								return `${ctx.dataset.label}: ${val < 0 ? '-' : ''}${formatted} ${currency}`;
							},
						},
					},
				},
				scales: {
					x: {
						stacked: true,
						grid: { display: false },
						ticks: {
							color: '#666',
							font: { size: 11 },
						},
						border: { display: false },
					},
					y: {
						grid: {
							color: 'rgba(255,255,255,0.05)',
						},
						ticks: {
							color: '#666',
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
		if (!_loading && chartCanvas && _m.length > 0) {
			untrack(() => buildChart());
		}
	});
</script>

{#if loading}
	<div class="loading">Loading...</div>
{:else if error}
	<div class="error-msg">{error}</div>
{:else if months.length === 0}
	<div class="loading">No data for the selected period.</div>
{:else}
	<div class="cashflow-page">
		<div class="chart-container">
			<canvas bind:this={chartCanvas}></canvas>
		</div>

		{#if activeMonth}
			<div class="month-title">
				{activeMonth.label}
			</div>

			<div class="summary-cards">
				<div class="card">
					<div class="card-value income">{formatAmount(activeMonth.income, activeMonth.currency)}</div>
					<div class="card-label">Income</div>
				</div>
				<div class="card">
					<div class="card-value expense">{formatAmount(activeMonth.expenses, activeMonth.currency)}</div>
					<div class="card-label">Expenses</div>
				</div>
				<div class="card">
					<div class="card-value" class:positive={activeMonth.net >= 0} class:negative={activeMonth.net < 0}>
						{formatAmount(activeMonth.net, activeMonth.currency)}
					</div>
					<div class="card-label">Net income</div>
				</div>
				<div class="card">
					<div class="card-value" class:positive={savingsRate > 0} class:negative={savingsRate < 0}>
						{savingsRate}%
					</div>
					<div class="card-label">Margin</div>
				</div>
			</div>

			<div class="breakdowns">
				<Collapsible.Root bind:open={incomeOpen}>
					<div class="section-header">
						<Collapsible.Trigger class="section-toggle">
							<span class="caret" class:open={incomeOpen}>&#9654;</span>
							Income
						</Collapsible.Trigger>
					</div>
					<Collapsible.Content>
						<div class="breakdown-list">
							{#each sortedIncome as [account, amount]}
								<div class="breakdown-row">
									<button class="breakdown-name" type="button" onclick={() => navigateToAccount(account)}>
										{shortAccountName(account)}
									</button>
									<span class="breakdown-amount income">
										{formatAmount(amount, activeMonth.currency)} ({pctOfTotal(amount, activeMonth.income)})
									</span>
								</div>
							{/each}
						</div>
					</Collapsible.Content>
				</Collapsible.Root>

				<Collapsible.Root bind:open={expenseOpen}>
					<div class="section-header">
						<Collapsible.Trigger class="section-toggle">
							<span class="caret" class:open={expenseOpen}>&#9654;</span>
							Expenses
						</Collapsible.Trigger>
					</div>
					<Collapsible.Content>
						<div class="breakdown-list">
							{#each sortedExpenses as [account, amount]}
								<div class="breakdown-row">
									<button class="breakdown-name" type="button" onclick={() => navigateToAccount(account)}>
										{shortAccountName(account)}
									</button>
									<span class="breakdown-amount expense">
										{formatAmount(amount, activeMonth.currency)} ({pctOfTotal(amount, activeMonth.expenses)})
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
	.cashflow-page {
		padding: 0.5rem 0.75rem;
	}

	.chart-container {
		height: 280px;
		padding: 0.75rem;
		background: var(--surface-card);
		border-radius: var(--radius-card);
		margin-bottom: 1rem;
	}

	.month-title {
		font-size: 1.1rem;
		font-weight: 600;
		padding: 0.5rem 0.75rem;
	}

	.summary-cards {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 0.75rem;
		padding: 0.5rem 0.75rem;
		margin-bottom: 0.75rem;
	}

	.card {
		text-align: center;
		padding: 0.75rem 0.5rem;
		background: var(--surface-card);
		border-radius: var(--radius-card);
	}

	.card-value {
		font-size: 1rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		margin-bottom: 0.25rem;
	}

	.card-label {
		font-size: var(--text-xs);
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.card-value.income, .breakdown-amount.income { color: #4adbc0; }
	.card-value.expense, .breakdown-amount.expense { color: #d46ab5; }
	.card-value.positive { color: #4adbc0; }
	.card-value.negative { color: #d46ab5; }

	.breakdowns {
		padding: 0 0.75rem;
	}

	.section-header {
		display: flex;
		align-items: baseline;
		padding: 0.75rem 0 0.4rem;
		border-bottom: 1px solid var(--border-subtle);
		margin-top: 0.5rem;
	}

	:global(.section-toggle) {
		background: none;
		border: none;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-base);
		font-weight: 600;
		cursor: pointer;
		padding: 0;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.caret {
		font-size: 0.5rem;
		color: var(--text-dim);
		transition: transform var(--transition-fast);
		display: inline-block;
	}

	.caret.open {
		transform: rotate(90deg);
	}

	.breakdown-list {
		padding: 0.25rem 0 0.5rem;
	}

	.breakdown-row {
		display: flex;
		align-items: baseline;
		gap: 0.25rem;
		padding: 0.3rem 0.75rem;
		font-size: var(--text-sm);
		border-radius: 0.25rem;
		transition: background var(--transition-fast);
	}

	.breakdown-row:hover {
		background: var(--surface-card);
	}

	.breakdown-name {
		flex: 1;
		min-width: 0;
		background: none;
		border: none;
		font: inherit;
		color: inherit;
		cursor: pointer;
		padding: 0;
		text-align: left;
	}

	.breakdown-name:hover {
		color: var(--text-primary);
	}

	.breakdown-amount {
		margin-left: auto;
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}

	@media (max-width: 640px) {
		.summary-cards {
			grid-template-columns: repeat(2, 1fr);
		}

		.chart-container {
			height: 200px;
		}
	}
</style>
