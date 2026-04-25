<script lang="ts">
	import { getBusinessSettings, type EntityRow, type ServiceRow, type BusinessDefaults } from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';

	let entities: EntityRow[] = $state([]);
	let services: ServiceRow[] = $state([]);
	let defaults: BusinessDefaults | null = $state(null);
	let loading = $state(true);
	let error = $state('');

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await getBusinessSettings();
			entities = resp.entities;
			services = resp.services;
			defaults = resp.defaults;
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		$selectedLedger;
		load();
	});

	function formatRate(rate: number): string {
		return rate.toLocaleString(undefined, {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		});
	}

	function typeLabel(t: string): string {
		const labels: Record<string, string> = {
			hours: 'per hour',
			days: 'per day',
			flat: 'flat rate',
			other: 'variable',
		};
		return labels[t] || t;
	}
</script>

<div class="settings-content">
	{#if loading}
		<div class="loading">Loading...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else if !defaults}
		<div class="empty">No invoicing configuration found.</div>
	{:else}
		<div class="settings-sections">
			<section class="settings-section">
				<h2>Defaults</h2>
				<div class="field-list">
					<div class="field-row">
						<span class="field-label">Currency</span>
						<span class="field-value">{defaults.currency}</span>
					</div>
					<div class="field-row">
						<span class="field-label">Default entity</span>
						<span class="field-value">{defaults.default_entity}</span>
					</div>
					<div class="field-row">
						<span class="field-label">A/R account</span>
						<span class="field-value mono">{defaults.default_ar_account}</span>
					</div>
					<div class="field-row">
						<span class="field-label">Bank account</span>
						<span class="field-value mono">{defaults.default_bank_account}</span>
					</div>
					<div class="field-row">
						<span class="field-label">Invoice output</span>
						<span class="field-value mono">{defaults.invoice_output}</span>
					</div>
					<div class="field-row">
						<span class="field-label">Next invoice #</span>
						<span class="field-value">{defaults.next_invoice_number}</span>
					</div>
					{#if defaults.days_until_overdue > 0}
						<div class="field-row">
							<span class="field-label">Days until overdue</span>
							<span class="field-value">{defaults.days_until_overdue}</span>
						</div>
					{/if}
					{#if defaults.notifications}
						<div class="field-row">
							<span class="field-label">Notifications</span>
							<span class="field-value">{defaults.notifications}</span>
						</div>
					{/if}
				</div>
			</section>

			<section class="settings-section">
				<h2>Entities</h2>
				{#if entities.length === 0}
					<div class="empty-inline">No entities configured.</div>
				{:else}
					<div class="card-grid">
						{#each entities as entity (entity.key)}
							<div class="settings-card">
								<div class="card-title">
									<span>{entity.name}</span>
									<span class="card-key">{entity.key}</span>
								</div>
								<div class="card-fields">
									{#if entity.email}
										<div class="field-row">
											<span class="field-label">Email</span>
											<span class="field-value">{entity.email}</span>
										</div>
									{/if}
									{#if entity.address}
										<div class="field-row">
											<span class="field-label">Address</span>
											<span class="field-value address">{entity.address}</span>
										</div>
									{/if}
									{#if entity.currency}
										<div class="field-row">
											<span class="field-label">Currency</span>
											<span class="field-value">{entity.currency}</span>
										</div>
									{/if}
									{#if entity.ar_account}
										<div class="field-row">
											<span class="field-label">A/R account</span>
											<span class="field-value mono">{entity.ar_account}</span>
										</div>
									{/if}
									{#if entity.bank_account}
										<div class="field-row">
											<span class="field-label">Bank account</span>
											<span class="field-value mono">{entity.bank_account}</span>
										</div>
									{/if}
									{#if entity.payment_instructions}
										<div class="field-row">
											<span class="field-label">Payment</span>
											<span class="field-value pre">{entity.payment_instructions}</span>
										</div>
									{/if}
									{#if entity.logo}
										<div class="field-row">
											<span class="field-label">Logo</span>
											<span class="field-value mono">{entity.logo}</span>
										</div>
									{/if}
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</section>

			<section class="settings-section">
				<h2>Services</h2>
				{#if services.length === 0}
					<div class="empty-inline">No services configured.</div>
				{:else}
					<div class="svc-list">
						<div class="svc-header">
							<span class="svc-name">Service</span>
							<span class="svc-type">Type</span>
							<span class="svc-rate">Rate</span>
							<span class="svc-account">Income account</span>
						</div>
						{#each services as svc (svc.key)}
							<div class="svc-row">
								<span class="svc-name">
									{svc.display_name}
									<span class="svc-key">{svc.key}</span>
								</span>
								<span class="svc-type">{typeLabel(svc.type)}</span>
								<span class="svc-rate">${formatRate(svc.rate)}</span>
								<span class="svc-account">{svc.income_account || '-'}</span>
							</div>
						{/each}
					</div>
				{/if}
			</section>
		</div>
	{/if}
</div>

<style>
	.settings-content {
		padding: 0.5rem;
	}

	.settings-sections {
		display: flex;
		flex-direction: column;
		gap: 1.5rem;
		padding: 0.25rem;
	}

	.settings-section h2 {
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
		margin: 0 0 0.5rem 0.25rem;
	}

	/* Defaults field list */
	.field-list {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.5rem 0.75rem;
	}

	.field-row {
		display: flex;
		gap: 0.75rem;
		padding: 0.25rem 0;
		font-size: var(--text-sm);
		line-height: 1.4;
	}

	.field-label {
		color: var(--text-dim);
		flex-shrink: 0;
		min-width: 8rem;
	}

	.field-value {
		color: var(--text-secondary);
		word-break: break-word;
	}

	.field-value.mono {
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
		font-size: var(--text-xs);
	}

	.field-value.address,
	.field-value.pre {
		white-space: pre-line;
	}

	/* Entity cards */
	.card-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, calc(33.333% - 0.5rem)));
		gap: 0.75rem;
	}

	.settings-card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		overflow: hidden;
	}

	.card-title {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.6rem 0.75rem;
		border-bottom: 1px solid var(--border-subtle);
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
	}

	.card-key {
		font-size: var(--text-xs);
		font-weight: 400;
		color: var(--text-dim);
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
	}

	.card-fields {
		padding: 0.5rem 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}

	/* Services table */
	.svc-list {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		overflow: hidden;
	}

	.svc-header {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		padding: 0.4rem 0.75rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		border-bottom: 1px solid var(--border-subtle);
	}

	.svc-row {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		padding: 0.4rem 0.75rem;
		font-size: var(--text-sm);
		border-bottom: 1px solid var(--border-subtle);
	}

	.svc-row:last-child {
		border-bottom: none;
	}

	.svc-name {
		flex: 1;
		min-width: 0;
		color: var(--text-primary);
	}

	.svc-key {
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
		margin-left: 0.4rem;
	}

	.svc-type {
		flex-shrink: 0;
		color: var(--text-dim);
		font-size: var(--text-xs);
		min-width: 4rem;
	}

	.svc-rate {
		flex-shrink: 0;
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--text-secondary);
		min-width: 5rem;
	}

	.svc-account {
		flex-shrink: 0;
		color: var(--text-dim);
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
		font-size: var(--text-xs);
		min-width: 10rem;
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-base);
		padding: 2rem 1rem;
		text-align: center;
	}

	.empty-inline {
		color: var(--text-dim);
		font-size: var(--text-sm);
		padding: 0.5rem 0.25rem;
	}

	@media (max-width: 640px) {
		.card-grid {
			grid-template-columns: 1fr;
		}

		.field-label {
			min-width: 6rem;
		}

		.svc-account { display: none; }
		.svc-type { display: none; }
	}
</style>
