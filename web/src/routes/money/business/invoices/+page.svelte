<script lang="ts">
	import { getInvoices, getInvoiceDetails, type InvoiceRow, type InvoiceDetailItem } from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';

	let invoices: InvoiceRow[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let invoiceCount = $state(0);
	let outstandingCount = $state(0);
	let sortAsc = $state(false);

	let expandedKeys = $state(new Set<string>());
	let detailsCache = $state(new Map<string, InvoiceDetailItem[]>());
	let detailsLoading = $state(new Set<string>());

	async function toggleExpand(inv: InvoiceRow) {
		const key = inv.invoice_number;
		if (expandedKeys.has(key)) {
			const next = new Set(expandedKeys);
			next.delete(key);
			expandedKeys = next;
			return;
		}

		const nextExpanded = new Set(expandedKeys);
		nextExpanded.add(key);
		expandedKeys = nextExpanded;

		if (!detailsCache.has(key)) {
			const nextLoading = new Set(detailsLoading);
			nextLoading.add(key);
			detailsLoading = nextLoading;
			try {
				const resp = await getInvoiceDetails(key);
				const nextCache = new Map(detailsCache);
				nextCache.set(key, resp.items);
				detailsCache = nextCache;
			} catch {
				// stay expanded but empty
			} finally {
				const nl = new Set(detailsLoading);
				nl.delete(key);
				detailsLoading = nl;
			}
		}
	}

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await getInvoices({ show_all: true });
			invoices = resp.invoices;
			invoiceCount = resp.invoice_count;
			outstandingCount = resp.outstanding_count;
			expandedKeys = new Set();
			detailsCache = new Map();
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Failed to load invoices';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		$selectedLedger;
		load();
	});

	let sorted = $derived.by(() => {
		const copy = [...invoices];
		copy.sort((a, b) => {
			const cmp = a.date.localeCompare(b.date);
			return sortAsc ? cmp : -cmp;
		});
		return copy;
	});

	function toggleSort() {
		sortAsc = !sortAsc;
	}

	function displayStatus(status: string): string {
		if (status === 'outstanding') return 'posted';
		return status;
	}

	function formatDate(iso: string): string {
		try {
			const d = new Date(iso + 'T00:00:00');
			return d.toLocaleDateString(undefined, {
				month: 'short',
				day: 'numeric',
				year: 'numeric',
			});
		} catch {
			return iso;
		}
	}

	function formatAmount(value: number): string {
		return value.toLocaleString(undefined, {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		});
	}

	function formatQty(value: number): string {
		if (value === Math.floor(value)) return String(value);
		return value.toFixed(2);
	}
</script>

<div class="invoices-content">
	{#if !loading}
		<div class="invoices-toolbar">
			<span class="result-count">{invoiceCount} invoices ({outstandingCount} outstanding)</span>
		</div>
	{/if}

	{#if loading}
		<div class="loading">Loading...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else if invoices.length === 0}
		<div class="empty">No invoices found.</div>
	{:else}
		<div class="inv-list">
			<div class="inv-header">
				<span class="inv-number">Invoice</span>
				<span class="inv-client">Client</span>
				<button class="inv-date sortable" onclick={toggleSort} type="button" title="Sort by date">
					Date <span class="sort-arrow">{sortAsc ? '\u25B2' : '\u25BC'}</span>
				</button>
				<span class="inv-status">Status</span>
				<span class="inv-amount">Amount</span>
				<span class="inv-expand-spacer"></span>
			</div>
			{#each sorted as inv (inv.invoice_number)}
				{@const key = inv.invoice_number}
				{@const isExpanded = expandedKeys.has(key)}
				<div class="inv-row" class:expanded={isExpanded}>
					<span class="inv-number">{inv.invoice_number}</span>
					<span class="inv-client">{inv.client}</span>
					<span class="inv-date">{formatDate(inv.date)}</span>
					<span
						class="inv-status"
						class:status-paid={inv.status === 'paid'}
						class:status-posted={inv.status === 'outstanding'}
						class:status-draft={inv.status === 'draft'}
					>{displayStatus(inv.status)}</span>
					<span class="inv-amount">${formatAmount(inv.total)}</span>
					<button
						class="inv-expand"
						onclick={() => toggleExpand(inv)}
						type="button"
						title={isExpanded ? 'Collapse' : 'Show line items'}
					>&#8943;</button>
				</div>
				{#if isExpanded}
					<div class="inv-details">
						{#if detailsLoading.has(key)}
							<div class="detail-row"><span class="detail-desc">Loading...</span></div>
						{:else if detailsCache.has(key)}
							{#each detailsCache.get(key) ?? [] as item}
								<div class="detail-row">
									<span class="detail-desc">
										{item.description}
										{#if item.detail}
											<span class="detail-note">{item.detail}</span>
										{/if}
									</span>
									<span class="detail-qty">{formatQty(item.quantity)} &times; ${formatAmount(item.rate)}</span>
									{#if item.discount > 0}
										<span class="detail-discount">-${formatAmount(item.discount)}</span>
									{/if}
									<span class="detail-amount">${formatAmount(item.amount)}</span>
								</div>
							{/each}
						{/if}
					</div>
				{/if}
			{/each}
		</div>
	{/if}
</div>

<style>
	.invoices-content {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-height: 0;
	}

	.invoices-toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.4rem 1rem;
		flex-shrink: 0;
	}

	.result-count {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.inv-list {
		flex: 1;
		overflow-y: auto;
		padding: 0 0.5rem 0.5rem;
	}

	.inv-list::-webkit-scrollbar { width: 4px; }
	.inv-list::-webkit-scrollbar-track { background: transparent; }
	.inv-list::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.inv-header {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		padding: 0.3rem 0.75rem 0.4rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
		border-bottom: 1px solid var(--border-subtle);
		margin-bottom: 0.15rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
	}

	.inv-header .inv-status {
		color: var(--text-dim);
		background: none;
		padding: 0;
	}

	.inv-expand-spacer {
		width: 1.1rem;
		flex-shrink: 0;
	}

	.inv-row {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		padding: 0.4rem 0.75rem;
		font-size: var(--text-sm);
		border-radius: 0.25rem;
		transition: background var(--transition-fast);
	}

	.inv-row:hover {
		background: var(--surface-card);
	}

	.inv-row.expanded {
		background: var(--surface-card);
	}

	.inv-number {
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
		font-size: var(--text-xs);
		color: var(--text-muted);
		flex-shrink: 0;
		min-width: 6.5rem;
	}

	.inv-client {
		flex: 1;
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		color: var(--text-primary);
		font-weight: 500;
	}

	.inv-date {
		color: var(--text-dim);
		white-space: nowrap;
		flex-shrink: 0;
		font-size: var(--text-xs);
	}

	button.sortable {
		background: none;
		border: none;
		color: var(--text-dim);
		font: inherit;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		cursor: pointer;
		padding: 0;
	}

	button.sortable:hover {
		color: var(--text-muted);
	}

	.sort-arrow {
		font-size: 0.55rem;
		vertical-align: middle;
		margin-left: 0.15rem;
		opacity: 0.5;
	}

	button.sortable:hover .sort-arrow {
		opacity: 1;
	}

	.inv-status {
		font-size: var(--text-xs);
		flex-shrink: 0;
		padding: 0.1rem 0.4rem;
		border-radius: var(--radius-pill);
	}

	.inv-status.status-posted {
		color: #e8b84a;
		background: rgba(232, 184, 74, 0.12);
	}

	.inv-status.status-paid {
		color: #4adbc0;
		background: rgba(74, 219, 192, 0.12);
	}

	.inv-status.status-draft {
		color: var(--text-muted);
		background: rgba(136, 136, 136, 0.12);
	}

	.inv-amount {
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
		flex-shrink: 0;
		min-width: 5.5rem;
		color: var(--text-primary);
	}

	.inv-expand {
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

	.inv-expand:hover {
		color: var(--text-muted);
	}

	.inv-details {
		padding: 0.15rem 0.75rem 0.4rem 2.5rem;
		background: var(--surface-card);
		border-radius: 0 0 0.25rem 0.25rem;
		margin-top: -0.15rem;
	}

	.detail-row {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		padding: 0.15rem 0;
		font-size: var(--text-xs);
	}

	.detail-desc {
		flex: 1;
		min-width: 0;
		color: var(--text-secondary);
	}

	.detail-note {
		color: var(--text-dim);
		margin-left: 0.25rem;
	}

	.detail-qty {
		color: var(--text-dim);
		white-space: nowrap;
		flex-shrink: 0;
	}

	.detail-discount {
		color: #d46ab5;
		white-space: nowrap;
		flex-shrink: 0;
	}

	.detail-amount {
		margin-left: auto;
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
		color: var(--text-secondary);
		min-width: 4rem;
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-base);
		padding: 2rem 1rem;
		text-align: center;
	}

	@media (max-width: 640px) {
		.inv-date { display: none; }
		.inv-header .inv-status,
		.inv-row .inv-status { display: none; }
		.inv-number { min-width: 5rem; }
		.inv-amount { min-width: 4rem; }
		.inv-details { padding-left: 1.5rem; }
		.detail-qty { display: none; }
	}
</style>
