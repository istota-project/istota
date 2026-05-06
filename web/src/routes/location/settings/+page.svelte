<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getModuleServices,
		getLocationSettingsInfo,
		type ServiceCard as ServiceCardData,
		type LocationSettingsInfo,
	} from '$lib/api';
	import { ServiceCard, SettingsLayout, SettingsCard } from '$lib/components/settings';

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

<SettingsLayout
	title="Location settings"
	description="Overland GPS connection and place-detection tuning. The ingest token is encrypted at rest and never sent back to the browser."
	{loading}
	{error}
>
	{#if !moduleEnabled}
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
			<SettingsCard title="Webhook URL">
				<p class="hint">
					Paste this into the Overland app, replacing
					<code>&lt;token&gt;</code> with the ingest token you saved
					above. The token never leaves the server, so the URL shown
					here uses a placeholder.
				</p>
				<code class="webhook-url">{info.webhook_url}</code>
			</SettingsCard>

			<SettingsCard
				title="Place detection"
				description="Read-only — these knobs are tuned instance-wide."
			>
				<dl class="kv">
					<dt>Accuracy threshold (m)</dt>
					<dd>{info.place_detection.accuracy_threshold_m}</dd>
					<dt>Visit exit (min)</dt>
					<dd>{info.place_detection.visit_exit_minutes}</dd>
				</dl>
			</SettingsCard>
		{/if}
	{/if}
</SettingsLayout>

<style>
	/* Shared .settings/.card/.field/.banner primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only location-
	   specific styling (webhook URL display, kv list) stays. */

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
</style>
