<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getModuleServices,
		getLocationSettingsInfo,
		type ServiceCard as ServiceCardData,
		type LocationSettingsInfo,
	} from '$lib/api';
	import { ServiceCard } from '$lib/components/settings';

	let loading = $state(true);
	let error = $state('');

	let moduleServices: ServiceCardData[] = $state([]);
	let moduleEnabled = $state(true);
	let info: LocationSettingsInfo | null = $state(null);

	async function refresh() {
		loading = true;
		error = '';
		try {
			const [mod, settings] = await Promise.all([
				getModuleServices('location'),
				getLocationSettingsInfo(),
			]);
			moduleEnabled = mod.module_enabled;
			moduleServices = mod.services;
			info = settings;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	async function reloadServices() {
		try {
			const mod = await getModuleServices('location');
			moduleServices = mod.services;
			moduleEnabled = mod.module_enabled;
		} catch {
			// non-fatal
		}
	}

	onMount(refresh);
</script>

<div class="settings">
	<header class="settings-header">
		<div>
			<h1>Location settings</h1>
			<p class="hint">
				Overland GPS connection and place-detection tuning. The ingest
				token is encrypted at rest and never sent back to the browser.
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
			Location module is disabled. Enable it in
			<a href="{base}/settings">Settings → Preferences</a> to manage GPS
			ingest.
		</div>
	{:else}
		{#each moduleServices as svc (svc.service)}
			<ServiceCard service={svc} onChanged={reloadServices} />
		{/each}

		{#if info}
			<section class="card">
				<header class="section-header">
					<h2>Webhook URL</h2>
				</header>
				<p class="hint">
					Paste this into the Overland app, replacing
					<code>&lt;token&gt;</code> with the ingest token you saved
					above. The token never leaves the server, so the URL shown
					here uses a placeholder.
				</p>
				<code class="webhook-url">{info.webhook_url}</code>
			</section>

			<section class="card">
				<header class="section-header">
					<h2>Place detection</h2>
				</header>
				<p class="hint">
					Read-only — these knobs are tuned instance-wide.
				</p>
				<dl class="kv">
					<dt>Accuracy threshold (m)</dt>
					<dd>{info.place_detection.accuracy_threshold_m}</dd>
					<dt>Visit exit (min)</dt>
					<dd>{info.place_detection.visit_exit_minutes}</dd>
				</dl>
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
	.banner.info {
		background: rgba(110, 184, 132, 0.15);
		color: #8d8;
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
	}

	.webhook-url {
		display: block;
		padding: 0.4rem 0.6rem;
		font-family: ui-monospace, SFMono-Regular, monospace;
		word-break: break-all;
	}

	.kv {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 0.75rem;
		margin: 0;
		font-size: var(--text-sm);
	}

	.kv dt {
		color: var(--text-dim);
	}

	.kv dd {
		margin: 0;
		color: var(--text-secondary);
	}

	@media (max-width: 768px) {
		.settings {
			padding: 1rem 0.75rem 3rem;
		}
		.card {
			padding: 0.75rem;
		}
	}
</style>
