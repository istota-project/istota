<script lang="ts">
	import type { MoneymanLedger } from '$lib/api';
	import { selectedService } from '$lib/stores/services';

	interface FavaDetail {
		ledgers: MoneymanLedger[];
		favaPrefix: string | null;
	}

	let svc = $derived($selectedService);

	let fava = $derived(
		svc?.id === 'fava' ? (svc.detail as FavaDetail | null) : null,
	);
</script>

<div class="svc-detail">
	{#if !svc}
		<div class="empty">Select a service</div>
	{:else if svc.status === 'loading'}
		<div class="empty">Loading...</div>
	{:else if svc.status === 'error'}
		<div class="empty error">Failed to load {svc.name}</div>
	{:else if svc.id === 'fava' && fava}
		<div class="detail-card">
			<div class="detail-header">
				<h2>{svc.name}</h2>
				<span class="detail-desc">{svc.description}</span>
			</div>

			{#if fava.ledgers.length > 0}
				<div class="detail-section">
					<div class="section-label">Ledgers</div>
					<ul class="ledger-list">
						{#each fava.ledgers as ledger}
							<li>{ledger.name}</li>
						{/each}
					</ul>
				</div>
			{/if}

			{#if fava.favaPrefix}
				<div class="detail-actions">
					<a href={fava.favaPrefix} class="action-btn">Open Fava</a>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.svc-detail {
		padding: 1.5rem;
		flex: 1;
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-sm);
	}

	.empty.error {
		color: #c66;
	}

	.detail-card {
		max-width: 480px;
	}

	.detail-header {
		margin-bottom: 1.5rem;
	}

	.detail-header h2 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0 0 0.25rem;
	}

	.detail-desc {
		font-size: var(--text-sm);
		color: var(--text-muted);
	}

	.detail-section {
		margin-bottom: 1.25rem;
	}

	.section-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		margin-bottom: 0.4rem;
	}

	.ledger-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}

	.ledger-list li {
		font-size: var(--text-sm);
		padding: 0.3rem 0;
		color: var(--text-secondary);
	}

	.detail-actions {
		margin-top: 1.5rem;
	}

	.action-btn {
		display: inline-block;
		font-size: var(--text-sm);
		color: var(--text-primary);
		text-decoration: none;
		padding: 0.4rem 0.85rem;
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		transition: background var(--transition-fast), border-color var(--transition-fast);
	}

	.action-btn:hover {
		background: var(--surface-raised);
		border-color: var(--text-dim);
	}
</style>
