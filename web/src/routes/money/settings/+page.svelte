<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { getModuleServices, type ServiceCard as ServiceCardData } from '$lib/api';
	import {
		getBusinessSettings,
		type EntityRow,
		type ServiceRow,
		type BusinessDefaults,
	} from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';
	import { ServiceCard } from '$lib/components/settings';

	let loading = $state(true);
	let error = $state('');

	let moduleServices: ServiceCardData[] = $state([]);
	let moduleEnabled = $state(true);

	let entities: EntityRow[] = $state([]);
	let services: ServiceRow[] = $state([]);
	let defaults: BusinessDefaults | null = $state(null);
	let businessError = $state('');

	async function loadServices() {
		const mod = await getModuleServices('money');
		moduleServices = mod.services;
		moduleEnabled = mod.module_enabled;
	}

	async function loadBusiness() {
		try {
			const resp = await getBusinessSettings();
			entities = resp.entities;
			services = resp.services;
			defaults = resp.defaults;
			businessError = '';
		} catch (e) {
			businessError =
				e instanceof Error ? e.message : 'Failed to load business settings';
		}
	}

	async function refresh() {
		loading = true;
		error = '';
		try {
			await Promise.all([loadServices(), loadBusiness()]);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	onMount(refresh);

	$effect(() => {
		$selectedLedger;
		void loadBusiness();
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

<div class="settings">
	<header class="settings-header">
		<div>
			<h1>Money settings</h1>
			<p class="hint">
				Monarch credentials and business configuration. Secrets are encrypted
				at rest and never sent back to the browser.
			</p>
		</div>
	</header>

	{#if error}
		<div class="banner error">{error}</div>
	{/if}

	{#if loading}
		<div class="placeholder">Loading…</div>
	{:else if !moduleEnabled}
		<div class="banner info">
			Money module is disabled. Enable it in
			<a href="{base}/settings">Settings → Preferences</a> to manage Monarch
			credentials and invoicing.
		</div>
	{:else}
		{#each moduleServices as svc (svc.service)}
			<ServiceCard service={svc} onChanged={loadServices} />
		{/each}

		<section class="card">
			<header class="section-header">
				<h2>Business defaults</h2>
			</header>
			{#if businessError}
				<div class="banner error">{businessError}</div>
			{:else if !defaults}
				<p class="empty">No invoicing configuration found.</p>
			{:else}
				<dl class="kv">
					<dt>Currency</dt><dd>{defaults.currency}</dd>
					<dt>Default entity</dt><dd>{defaults.default_entity}</dd>
					<dt>A/R account</dt><dd><code>{defaults.default_ar_account}</code></dd>
					<dt>Bank account</dt><dd><code>{defaults.default_bank_account}</code></dd>
					<dt>Invoice output</dt><dd><code>{defaults.invoice_output}</code></dd>
					<dt>Next invoice #</dt><dd>{defaults.next_invoice_number}</dd>
					{#if defaults.days_until_overdue > 0}
						<dt>Days until overdue</dt><dd>{defaults.days_until_overdue}</dd>
					{/if}
					{#if defaults.notifications}
						<dt>Notifications</dt><dd>{defaults.notifications}</dd>
					{/if}
				</dl>
			{/if}
		</section>

		{#if defaults}
			<section class="card">
				<header class="section-header">
					<h2>Entities ({entities.length})</h2>
				</header>
				<p class="hint">
					Read-only view from <code>INVOICING.md</code>. Edit on the server
					to change.
				</p>
				{#if entities.length === 0}
					<p class="empty">No entities configured.</p>
				{:else}
					<div class="entity-grid">
						{#each entities as entity (entity.key)}
							<div class="entity">
								<div class="entity-head">
									<span>{entity.name}</span>
									<span class="entity-key"><code>{entity.key}</code></span>
								</div>
								<dl class="kv compact">
									{#if entity.email}
										<dt>Email</dt><dd>{entity.email}</dd>
									{/if}
									{#if entity.address}
										<dt>Address</dt><dd class="pre">{entity.address}</dd>
									{/if}
									{#if entity.currency}
										<dt>Currency</dt><dd>{entity.currency}</dd>
									{/if}
									{#if entity.ar_account}
										<dt>A/R</dt><dd><code>{entity.ar_account}</code></dd>
									{/if}
									{#if entity.bank_account}
										<dt>Bank</dt><dd><code>{entity.bank_account}</code></dd>
									{/if}
									{#if entity.payment_instructions}
										<dt>Payment</dt><dd class="pre">{entity.payment_instructions}</dd>
									{/if}
									{#if entity.logo}
										<dt>Logo</dt><dd><code>{entity.logo}</code></dd>
									{/if}
								</dl>
							</div>
						{/each}
					</div>
				{/if}
			</section>

			<section class="card">
				<header class="section-header">
					<h2>Services ({services.length})</h2>
				</header>
				{#if services.length === 0}
					<p class="empty">No services configured.</p>
				{:else}
					<div class="table-scroll">
						<table class="grid">
							<thead>
								<tr>
									<th>Service</th>
									<th>Type</th>
									<th class="num">Rate</th>
									<th>Income account</th>
								</tr>
							</thead>
							<tbody>
								{#each services as svc (svc.key)}
									<tr>
										<td>
											{svc.display_name}
											<span class="muted">  <code>{svc.key}</code></span>
										</td>
										<td class="muted">{typeLabel(svc.type)}</td>
										<td class="num">${formatRate(svc.rate)}</td>
										<td class="muted"><code>{svc.income_account || '—'}</code></td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</section>
		{/if}
	{/if}
</div>

<style>
	.settings {
		width: 100%;
		max-width: 980px;
		margin: 0 auto;
		padding: 1.5rem 1rem 4rem;
		display: flex;
		flex-direction: column;
		gap: 1rem;
		box-sizing: border-box;
		container-type: inline-size;
		container-name: settings;
	}

	.settings-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1rem;
		flex-wrap: wrap;
	}

	.settings-header h1 {
		margin: 0;
		font-size: var(--text-lg, 1.05rem);
		color: var(--text-primary);
	}

	.hint {
		margin: 0.25rem 0 0;
		font-size: var(--text-sm);
		color: var(--text-muted);
		max-width: 60ch;
	}

	.hint code,
	code {
		background: var(--surface-raised);
		padding: 0 0.3rem;
		border-radius: 0.2rem;
		font-size: 0.8em;
	}

	.banner {
		padding: 0.4rem 0.75rem;
		border-radius: var(--radius-card);
		font-size: var(--text-sm);
	}
	.banner.error {
		background: rgba(204, 102, 102, 0.15);
		color: #e88;
	}

	.placeholder {
		color: var(--text-dim);
		padding: 2rem 0;
		text-align: center;
	}

	.card {
		background: var(--surface-card);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-card);
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.card h2 {
		margin: 0;
		font-size: var(--text-base);
		color: var(--text-primary);
	}

	.section-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.75rem;
		flex-wrap: wrap;
	}

	.empty {
		font-size: var(--text-sm);
		color: var(--text-dim);
		margin: 0;
	}

	.muted {
		color: var(--text-dim);
	}

	.kv {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 0.75rem;
		margin: 0;
		font-size: var(--text-sm);
	}

	.kv.compact {
		gap: 0.15rem 0.6rem;
		font-size: var(--text-xs);
	}

	.kv dt {
		color: var(--text-dim);
	}

	.kv dd {
		margin: 0;
		color: var(--text-secondary);
		word-break: break-word;
	}

	.kv dd.pre {
		white-space: pre-line;
	}

	.entity-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 0.6rem;
	}

	.entity {
		background: var(--surface-base);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.5rem 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}

	.entity-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
		font-weight: 600;
		color: var(--text-primary);
		font-size: var(--text-sm);
	}

	.entity-key {
		font-weight: 400;
		color: var(--text-dim);
		font-size: var(--text-xs);
	}

	.table-scroll {
		width: 100%;
		overflow-x: auto;
	}

	.grid {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--text-sm);
	}

	.grid th,
	.grid td {
		text-align: left;
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--border-subtle);
		vertical-align: middle;
	}

	.grid th {
		color: var(--text-dim);
		font-weight: 500;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.grid th.num,
	.grid td.num {
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}

	@media (max-width: 768px) {
		.settings {
			padding: 1rem 0.75rem 3rem;
		}
		.settings-header {
			flex-direction: column;
			align-items: stretch;
		}
		.card {
			padding: 0.75rem;
		}
	}

	@media (max-width: 640px) {
		.settings {
			padding: 0.75rem 0.5rem 3rem;
		}
		.card {
			padding: 0.6rem;
		}
	}
</style>
