<script lang="ts">
	import { getClients, type ClientRow } from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';

	let clients: ClientRow[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await getClients();
			clients = resp.clients;
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Failed to load clients';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		$selectedLedger;
		load();
	});
</script>

<div class="clients-content">
	{#if loading}
		<div class="loading">Loading...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else if clients.length === 0}
		<div class="empty">No clients configured.</div>
	{:else}
		<div class="client-grid">
			{#each clients as client (client.key)}
				<div class="client-card">
					<div class="card-header">
						<span class="client-name">{client.name}</span>
						<span class="client-key">{client.key}</span>
					</div>
					<div class="card-body">
						{#if client.email}
							<div class="card-field">
								<span class="field-label">Email</span>
								<span class="field-value">{client.email}</span>
							</div>
						{/if}
						{#if client.address}
							<div class="card-field">
								<span class="field-label">Address</span>
								<span class="field-value address">{client.address}</span>
							</div>
						{/if}
						<div class="card-field">
							<span class="field-label">Terms</span>
							<span class="field-value">{typeof client.terms === 'number' ? `Net ${client.terms}` : client.terms}</span>
						</div>
						<div class="card-field">
							<span class="field-label">Entity</span>
							<span class="field-value">{client.entity_name || client.entity}</span>
						</div>
						{#if client.schedule !== 'on-demand'}
							<div class="card-field">
								<span class="field-label">Schedule</span>
								<span class="field-value">{client.schedule}, day {client.schedule_day}</span>
							</div>
						{/if}
						<div class="card-field">
							<span class="field-label">A/R account</span>
							<span class="field-value account">{client.ar_account}</span>
						</div>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.clients-content {
		padding: 0.5rem;
	}

	.client-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
		gap: 0.75rem;
		padding: 0.25rem;
	}

	.client-card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		overflow: hidden;
		transition: border-color var(--transition-fast);
	}

	.client-card:hover {
		border-color: var(--text-dim);
	}

	.card-header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.6rem 0.75rem;
		border-bottom: 1px solid var(--border-subtle);
	}

	.client-name {
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
	}

	.client-key {
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
	}

	.card-body {
		padding: 0.5rem 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}

	.card-field {
		display: flex;
		gap: 0.5rem;
		font-size: var(--text-sm);
		line-height: 1.4;
	}

	.field-label {
		color: var(--text-dim);
		flex-shrink: 0;
		min-width: 5.5rem;
	}

	.field-value {
		color: var(--text-secondary);
		word-break: break-word;
	}

	.field-value.address {
		white-space: pre-line;
	}

	.field-value.account {
		font-size: var(--text-xs);
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-base);
		padding: 2rem 1rem;
		text-align: center;
	}

	@media (max-width: 640px) {
		.client-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
