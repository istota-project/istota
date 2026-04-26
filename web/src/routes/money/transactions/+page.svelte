<script lang="ts">
	import { getTransactions, getPostings, type TransactionRow, type PostingRow } from '$lib/money/api';
	import { selectedAccount, selectedYear, filterText } from '$lib/money/stores/transactions';
	import { selectedLedger } from '$lib/money/stores/ledger';
	import { displayBalance } from '$lib/money/utils/accounts';

	let transactions: TransactionRow[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let total = $state(0);
	let currentPage = $state(1);
	let perPage = 100;

	// Track expanded transactions and their postings
	let expandedKeys = $state(new Set<string>());
	let postingsCache = $state(new Map<string, PostingRow[]>());
	let postingsLoading = $state(new Set<string>());

	function txnKey(txn: TransactionRow): string {
		return `${txn.date}|${txn.payee}|${txn.narration}|${txn.account}|${txn.position}`;
	}

	async function toggleExpand(txn: TransactionRow) {
		const key = txnKey(txn);
		if (expandedKeys.has(key)) {
			const next = new Set(expandedKeys);
			next.delete(key);
			expandedKeys = next;
			return;
		}

		// Expand and fetch postings if not cached
		const nextExpanded = new Set(expandedKeys);
		nextExpanded.add(key);
		expandedKeys = nextExpanded;

		if (!postingsCache.has(key)) {
			const nextLoading = new Set(postingsLoading);
			nextLoading.add(key);
			postingsLoading = nextLoading;
			try {
				const resp = await getPostings({
					ledger: $selectedLedger || undefined,
					date: txn.date,
					payee: txn.payee,
					narration: txn.narration,
					account: txn.account,
					position: txn.position,
				});
				const nextCache = new Map(postingsCache);
				nextCache.set(key, resp.postings);
				postingsCache = nextCache;
			} catch {
				// Silently fail — row stays expanded but empty
			} finally {
				const nl = new Set(postingsLoading);
				nl.delete(key);
				postingsLoading = nl;
			}
		}
	}

	async function load(opts: {
		ledger: string; account: string; year: number; filter: string; page: number;
	}) {
		loading = true;
		error = '';
		try {
			const resp = await getTransactions({
				ledger: opts.ledger || undefined,
				account: opts.account || undefined,
				year: opts.year || undefined,
				filter: opts.filter || undefined,
				page: opts.page,
				per_page: perPage,
			});
			transactions = resp.transactions;
			total = resp.total;
			// Clear expand state on reload
			expandedKeys = new Set();
			postingsCache = new Map();
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Failed to load transactions';
		} finally {
			loading = false;
		}
	}

	function currentOpts(page: number) {
		return {
			ledger: $selectedLedger,
			account: $selectedAccount,
			year: $selectedYear,
			filter: $filterText,
			page,
		};
	}

	// Reload and reset to page 1 when any filter changes
	$effect(() => {
		const opts = currentOpts(1);
		currentPage = 1;
		load(opts);
	});

	function prevPage() {
		if (currentPage > 1) {
			currentPage--;
			load(currentOpts(currentPage));
		}
	}

	function nextPage() {
		if (currentPage * perPage < total) {
			currentPage++;
			load(currentOpts(currentPage));
		}
	}

	interface TxnGroup {
		date: string;
		rows: TransactionRow[];
	}

	let grouped = $derived.by(() => {
		const groups: TxnGroup[] = [];
		let lastDate = '';
		for (const txn of transactions) {
			if (txn.date !== lastDate) {
				groups.push({ date: txn.date, rows: [] });
				lastDate = txn.date;
			}
			groups[groups.length - 1].rows.push(txn);
		}
		return groups;
	});

	let totalPages = $derived(Math.max(1, Math.ceil(total / perPage)));

	function formatDate(iso: string): string {
		try {
			const d = new Date(iso + 'T00:00:00');
			return d.toLocaleDateString(undefined, {
				weekday: 'short',
				month: 'short',
				day: 'numeric',
				year: 'numeric',
			});
		} catch {
			return iso;
		}
	}

	function shortAccount(account: string): string {
		const parts = account.split(':');
		if (parts.length <= 2) return account;
		return parts.slice(-2).join(':');
	}
</script>

<div class="txn-content">
	{#if !loading}
		<div class="result-bar">
			<span class="result-count">{total.toLocaleString()} entries</span>
		</div>
	{/if}

	{#if loading && transactions.length === 0}
		<div class="loading">Loading...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else if transactions.length === 0}
		<div class="empty">No transactions found.</div>
	{:else}
		<div class="txn-scroll" class:faded={loading}>
			{#each grouped as group (group.date)}
				<div class="date-header">{formatDate(group.date)}</div>
				{#each group.rows as txn, i (group.date + '-' + i)}
					{@const key = txnKey(txn)}
					{@const isExpanded = expandedKeys.has(key)}
					<div class="txn-row" class:expanded={isExpanded}>
						<div class="txn-main">
							{#if txn.payee}
								<span class="txn-payee">{txn.payee}</span>
							{/if}
							{#if txn.narration}
								<span class="txn-narration">{txn.narration}</span>
							{/if}
						</div>
						<button
							class="txn-account"
							onclick={() => selectedAccount.set(txn.account)}
							type="button"
						>{shortAccount(txn.account)}</button>
						<span class="txn-amount" class:income={txn.account.startsWith('Income:')} class:expense={txn.account.startsWith('Expenses:')}>{displayBalance(txn.position, txn.account)}</span>
						<button
							class="txn-expand"
							onclick={() => toggleExpand(txn)}
							type="button"
							title={isExpanded ? 'Collapse' : 'Show all postings'}
						>&#8943;</button>
					</div>
					{#if isExpanded}
						<div class="postings">
							{#if postingsLoading.has(key)}
								<div class="posting-row"><span class="posting-account">Loading...</span></div>
							{:else if postingsCache.has(key)}
								{#each postingsCache.get(key) ?? [] as posting}
									<div class="posting-row">
										<button
											class="posting-account"
											onclick={() => selectedAccount.set(posting.account)}
											type="button"
										>{posting.account}</button>
										<span class="posting-amount">{posting.position}</span>
									</div>
								{/each}
							{/if}
						</div>
					{/if}
				{/each}
			{/each}
		</div>

		{#if totalPages > 1}
			<div class="pagination">
				<button onclick={prevPage} disabled={currentPage <= 1} type="button">&laquo; Prev</button>
				<span class="page-info">{currentPage} / {totalPages}</span>
				<button onclick={nextPage} disabled={currentPage >= totalPages} type="button">Next &raquo;</button>
			</div>
		{/if}
	{/if}
</div>

<style>
	.txn-content {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-height: 0;
	}

	.result-bar {
		padding: 0.4rem 0.75rem;
		flex-shrink: 0;
	}

	.result-count {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.txn-scroll {
		flex: 1;
		overflow-y: auto;
		padding: 0 0 0.5rem;
		transition: opacity var(--transition-fast);
	}

	.txn-scroll::-webkit-scrollbar { width: 4px; }
	.txn-scroll::-webkit-scrollbar-track { background: transparent; }
	.txn-scroll::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.txn-scroll.faded {
		opacity: 0.5;
	}

	.date-header {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		padding: 0.75rem 0.75rem 0.25rem;
		border-top: 1px solid var(--border-subtle);
		margin-top: 0.25rem;
	}

	.date-header:first-child {
		border-top: none;
		margin-top: 0;
	}

	.txn-row {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		padding: 0.3rem 0.75rem;
		font-size: var(--text-sm);
		border-radius: 0.25rem;
		transition: background var(--transition-fast);
	}

	.txn-row:hover {
		background: var(--surface-card);
	}

	.txn-row.expanded {
		background: var(--surface-card);
	}

	.txn-main {
		flex: 1;
		min-width: 0;
		display: flex;
		gap: 0.5rem;
		overflow: hidden;
	}

	.txn-payee {
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.txn-narration {
		color: var(--text-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.txn-account {
		background: none;
		border: none;
		color: var(--text-dim);
		font: inherit;
		font-size: var(--text-xs);
		white-space: nowrap;
		cursor: pointer;
		padding: 0;
		flex-shrink: 0;
	}

	.txn-account:hover {
		color: var(--text-muted);
	}

	.txn-amount {
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
		flex-shrink: 0;
		min-width: 6rem;
	}

	.txn-amount.income { color: #4adbc0; }
	.txn-amount.expense { color: #d46ab5; }

	.txn-expand {
		background: none;
		border: none;
		color: var(--text-dim);
		font-size: var(--text-sm);
		cursor: pointer;
		padding: 0 0.15rem;
		flex-shrink: 0;
		line-height: 1;
		letter-spacing: 0.1em;
	}

	.txn-expand:hover {
		color: var(--text-muted);
	}

	.postings {
		padding: 0.15rem 0.75rem 0.4rem 2.5rem;
		background: var(--surface-card);
		border-radius: 0 0 0.25rem 0.25rem;
		margin-top: -0.15rem;
	}

	.posting-row {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		padding: 0.15rem 0;
		font-size: var(--text-xs);
	}

	.posting-account {
		background: none;
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-xs);
		cursor: pointer;
		padding: 0;
		text-align: left;
	}

	.posting-account:hover {
		color: var(--text-secondary);
	}

	.posting-amount {
		margin-left: auto;
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
		color: var(--text-secondary);
	}

	.pagination {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 1rem;
		padding: 0.75rem;
		flex-shrink: 0;
		border-top: 1px solid var(--border-subtle);
	}

	.pagination button {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		color: var(--text-secondary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.3rem 0.75rem;
		cursor: pointer;
		transition: all var(--transition-fast);
	}

	.pagination button:hover:not(:disabled) {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.pagination button:disabled {
		opacity: 0.3;
		cursor: default;
	}

	.page-info {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-base);
		padding: 2rem 1rem;
		text-align: center;
	}

	@media (max-width: 640px) {
		.txn-account { display: none; }
		.txn-amount { min-width: 4rem; }
	}
</style>
