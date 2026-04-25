<script lang="ts">
	import { base } from '$app/paths';
</script>

<div class="dashboard">
	<div class="quick-links">
		<a href="{base}/accounts" class="link-card">
			<div class="link-title">Accounts</div>
			<div class="link-desc">View account balances</div>
		</a>
		<a href="{base}/transactions" class="link-card">
			<div class="link-title">Transactions</div>
			<div class="link-desc">Browse recent transactions</div>
		</a>
		<a href="{base}/reports/cash-flow" class="link-card">
			<div class="link-title">Cash flow</div>
			<div class="link-desc">Monthly income and expenses</div>
		</a>
		<a href="{base}/reports/income-statement" class="link-card">
			<div class="link-title">Income statement</div>
			<div class="link-desc">Income and expenses by account</div>
		</a>
		<a href="{base}/reports/balance-sheet" class="link-card">
			<div class="link-title">Balance sheet</div>
			<div class="link-desc">Assets, liabilities, and net worth</div>
		</a>
		<a href="{base}/business/invoices" class="link-card">
			<div class="link-title">Business</div>
			<div class="link-desc">Clients and invoices</div>
		</a>
		<a href="{base}/taxes" class="link-card">
			<div class="link-title">Taxes</div>
			<div class="link-desc">Estimated quarterly payments</div>
		</a>
	</div>
</div>

<style>
	.quick-links {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
		gap: 1rem;
	}

	.link-card {
		display: block;
		background: var(--surface-card);
		border-radius: var(--radius-card);
		padding: 1.25rem;
		text-decoration: none;
		transition: background var(--transition-fast);
	}

	.link-card:hover {
		background: var(--surface-raised);
	}

	.link-title {
		font-weight: 600;
		font-size: 0.9rem;
		margin-bottom: 0.25rem;
	}

	.link-desc {
		font-size: 0.8rem;
		color: var(--text-muted);
	}
</style>
