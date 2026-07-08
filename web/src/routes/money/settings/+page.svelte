<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getModuleServices,
		monarchLogin,
		type ServiceCard as ServiceCardData,
	} from '$lib/api';
	import {
		getBusinessSettings,
		type EntityRow,
		type ServiceRow,
		type BusinessDefaults,
	} from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';
	import { ServiceCard, SettingsLayout, SettingsCard } from '$lib/components/settings';
	import { Button } from '$lib/components/ui';

	let loading = $state(true);
	let error = $state('');

	let moduleServices: ServiceCardData[] = $state([]);
	let moduleEnabled = $state(true);

	let entities: EntityRow[] = $state([]);
	let services: ServiceRow[] = $state([]);
	let defaults: BusinessDefaults | null = $state(null);
	let businessError = $state('');

	// Programmatic-login form state. Plain bindings — values are POSTed to
	// /money/monarch/login and never persisted in the browser beyond the
	// in-memory component state.
	let loginEmail = $state('');
	let loginPassword = $state('');
	let loginMfa = $state('');
	let loginBusy = $state(false);
	let loginMessage = $state('');
	let loginErrorKind = $state<'' | 'auth' | 'mfa' | 'cloudflare' | 'captcha' | 'other'>('');

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

	async function submitLogin() {
		if (!loginEmail || !loginPassword) return;
		loginBusy = true;
		loginMessage = '';
		loginErrorKind = '';
		try {
			await monarchLogin(loginEmail, loginPassword, loginMfa);
			loginMessage = 'Logged in — session_id and csrftoken saved.';
			loginPassword = '';
			loginMfa = '';
			await loadServices();
		} catch (e) {
			const msg = e instanceof Error ? e.message : 'Login failed';
			// FastAPI HTTPException details flow through apiFetch's error
			// message — match on status hints we surface server-side.
			const lower = msg.toLowerCase();
			if (msg.includes('MFA required')) {
				loginErrorKind = 'mfa';
			} else if (lower.includes('captcha')) {
				loginErrorKind = 'captcha';
			} else if (lower.includes('cloudflare')) {
				loginErrorKind = 'cloudflare';
			} else if (msg.match(/HTTP 4\d\d/) || msg.includes('rejected')) {
				loginErrorKind = 'auth';
			} else {
				loginErrorKind = 'other';
			}
			loginMessage = msg;
		} finally {
			loginBusy = false;
		}
	}

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

<SettingsLayout
	title="Money settings"
	description="Monarch credentials and business configuration. Secrets are encrypted at rest and never sent back to the browser."
	{loading}
	{error}
>
	{#if !moduleEnabled}
		<div class="banner info">
			Money module is disabled. Enable it in
			<a href="{base}/settings">Settings → Preferences</a> to manage Monarch
			credentials and invoicing.
		</div>
	{:else}
		{#each moduleServices as svc (svc.service)}
			{#if svc.service === 'monarch'}
				<div class="monarch-help">
					<h3>Connect to Monarch Money</h3>
					<p>
						Monarch's API requires browser session cookies. Pick the
						method that works for your account:
					</p>

					<details open>
						<summary>Option A — Login with email and password</summary>
						<p class="hint">
							We call Monarch's <code>/auth/login/</code> on your behalf and
							store the resulting <code>session_id</code> + <code>csrftoken</code>.
							Your password is used once and never written to disk. If
							Cloudflare blocks the request from this server, fall back to
							Option B.
						</p>
						<form
							class="login-form"
							onsubmit={(e) => {
								e.preventDefault();
								void submitLogin();
							}}
						>
							<label>
								<span>Email</span>
								<input
									type="email"
									bind:value={loginEmail}
									autocomplete="off"
									disabled={loginBusy}
									required
								/>
							</label>
							<label>
								<span>Password</span>
								<input
									type="password"
									bind:value={loginPassword}
									autocomplete="off"
									disabled={loginBusy}
									required
								/>
							</label>
							<label>
								<span>MFA code <small>(if MFA enabled)</small></span>
								<input
									type="text"
									inputmode="numeric"
									pattern="[0-9]*"
									bind:value={loginMfa}
									autocomplete="off"
									disabled={loginBusy}
									placeholder="6-digit code"
								/>
							</label>
							<div class="login-actions">
								<Button
									variant="primary"
									size="sm"
									disabled={loginBusy || !loginEmail || !loginPassword}
									type="submit"
								>
									{loginBusy ? 'Logging in…' : 'Login & save cookies'}
								</Button>
							</div>
							{#if loginMessage}
								<div
									class="login-status"
									data-kind={loginErrorKind || 'ok'}
								>
									{loginMessage}
								</div>
							{/if}
						</form>
					</details>

					<details>
						<summary>Option B — Paste cookies from your browser</summary>
						<p class="hint">
							Use this when programmatic login is blocked by Cloudflare
							(common on cloud-hosted Istota deploys).
						</p>
						<ol>
							<li>Open <a href="https://app.monarch.com" target="_blank" rel="noopener noreferrer">app.monarch.com</a> in a logged-in browser tab.</li>
							<li>Open DevTools (Cmd/Ctrl+Option+I) → <strong>Application</strong> → <strong>Cookies</strong> → <code>https://api.monarch.com</code>.</li>
							<li>Copy the value of <code>session_id</code> into the field below.</li>
							<li>Copy the value of <code>csrftoken</code> into the field below.</li>
							<li>Click <strong>Save</strong>.</li>
						</ol>
					</details>

					<p class="legacy-note">
						Cookies are the only credential we store. They last months
						on a trusted-device login.
					</p>
				</div>
			{/if}
			<ServiceCard service={svc} onChanged={loadServices} />
		{/each}

		<SettingsCard title="Business defaults">
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
		</SettingsCard>

		{#if defaults}
			<SettingsCard title="Entities ({entities.length})">
				<p class="hint">
					Read-only view from <code>INVOICING.md</code>. Edit on the server
					to change.
				</p>
				{#if entities.length === 0}
					<p class="empty">No entities configured.</p>
				{:else}
					<div class="entity-grid card-grid">
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
			</SettingsCard>

			<SettingsCard title="Services ({services.length})">
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
			</SettingsCard>
		{/if}
	{/if}
</SettingsLayout>

<style>
	/* Shared .settings/.card/.field/.grid/.banner primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only money-specific
	   styling (kv, entity grid, numeric column tweaks) stays. */

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
		--card-min: 220px;
		--card-gap: 0.6rem;
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

	/* Money's services table sizes by content; shared .settings .grid uses
	   fixed layout, so opt back to auto here. */
	.grid {
		table-layout: auto;
	}

	.monarch-help {
		background: var(--surface-base);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.75rem 1rem;
		font-size: var(--text-sm);
		color: var(--text-secondary);
	}

	.monarch-help h3 {
		margin: 0 0 0.4rem;
		font-size: var(--text-sm);
		color: var(--text-primary);
	}

	.monarch-help p,
	.monarch-help ol {
		margin: 0.25rem 0;
	}

	.monarch-help ol {
		padding-left: 1.25rem;
	}

	.monarch-help li {
		margin: 0.1rem 0;
	}

	.monarch-help code {
		background: var(--surface-raised);
		padding: 0 0.25rem;
		border-radius: 0.2rem;
		font-size: 0.92em;
	}

	.monarch-help .legacy-note {
		margin-top: 0.5rem;
		color: var(--text-dim);
		font-size: var(--text-xs);
	}

	.monarch-help details {
		margin: 0.4rem 0;
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.5rem 0.75rem;
		background: var(--surface-raised);
	}

	.monarch-help summary {
		cursor: pointer;
		font-weight: 600;
		color: var(--text-primary);
	}

	.monarch-help .hint {
		color: var(--text-dim);
		font-size: var(--text-xs);
		margin: 0.3rem 0;
	}

	.login-form {
		display: grid;
		gap: 0.5rem;
		margin-top: 0.5rem;
	}

	.login-form label {
		display: grid;
		grid-template-columns: 120px 1fr;
		gap: 0.5rem;
		align-items: center;
		font-size: var(--text-sm);
	}

	.login-form label span {
		color: var(--text-dim);
	}

	.login-form input {
		font: inherit;
		padding: 0.35rem 0.5rem;
		border-radius: var(--radius-sm, 0.3rem);
		border: 1px solid var(--border-default);
		background: var(--surface-base);
		color: var(--text-primary);
	}

	.login-actions {
		display: flex;
		justify-content: flex-end;
	}

	.login-status {
		font-size: var(--text-xs);
		padding: 0.35rem 0.5rem;
		border-radius: var(--radius-sm, 0.3rem);
		border: 1px solid var(--border-default);
	}

	.login-status[data-kind='ok'] {
		color: #6eb884;
		border-color: #2d4a32;
	}

	.login-status[data-kind='auth'],
	.login-status[data-kind='other'] {
		color: var(--text-secondary);
		border-color: var(--border-default);
	}

	.login-status[data-kind='mfa'],
	.login-status[data-kind='cloudflare'] {
		color: var(--text-secondary);
		background: var(--surface-base);
	}

	.grid th.num,
	.grid td.num {
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}

	/* Light theme overrides — dark rules above untouched. */
	:global(:root[data-theme='light']) .login-status[data-kind='ok'] {
		color: #15803d;
		border-color: #b6e0c2;
	}
</style>
