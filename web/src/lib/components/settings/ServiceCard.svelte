<script lang="ts">
	import { Button } from '$lib/components/ui';
	import type { ServiceCard as ServiceCardData } from '$lib/api';
	import SecretField from './SecretField.svelte';

	interface Props {
		service: ServiceCardData;
		onChanged?: () => void;
		// OAuth services (google_workspace) hide the secret form and show a
		// Connect/Disconnect button. The host page wires the click handlers
		// since the routes are app-specific.
		onConnect?: () => void;
		onDisconnect?: () => void;
		oauthBusy?: boolean;
	}

	let {
		service,
		onChanged,
		onConnect,
		onDisconnect,
		oauthBusy = false,
	}: Props = $props();

	function statusLabel(s: ServiceCardData['status']): string {
		switch (s) {
			case 'configured': return 'Configured';
			case 'partial':    return 'Partial';
			case 'missing':    return 'Missing';
			case 'unavailable':return 'Not enabled';
		}
	}

	let oauthConnected = $derived(service.oauth && service.connected === true);
</script>

<section class="card" data-status={service.status}>
	<header class="section-header">
		<div class="title">
			<h2>{service.label}</h2>
			{#if service.oauth}
				<span class="status-pill status-{oauthConnected ? 'configured' : 'missing'}">
					{oauthConnected ? 'Connected' : 'Not connected'}
				</span>
			{:else}
				<span class="status-pill status-{service.status}">
					{statusLabel(service.status)}
				</span>
			{/if}
		</div>
		{#if service.last_updated}
			<span class="meta">Updated {service.last_updated}</span>
		{/if}
	</header>

	{#if service.used_by && service.used_by.length > 0}
		<p class="used-by">
			Used by:
			{#each service.used_by as skill, i (skill)}
				{#if i > 0},{' '}{/if}
				<code>{skill}</code>
			{/each}
		</p>
	{/if}

	{#if service.oauth}
		{#if service.enabled === false}
			<p class="empty">
				Google Workspace OAuth is not configured on this Istota instance.
			</p>
		{:else if oauthConnected}
			<div class="oauth-actions">
				<Button variant="ghost" size="sm" onclick={onDisconnect} disabled={oauthBusy}>
					{oauthBusy ? 'Disconnecting…' : 'Disconnect'}
				</Button>
			</div>
		{:else}
			<div class="oauth-actions">
				<Button variant="primary" size="sm" onclick={onConnect} disabled={oauthBusy}>
					{oauthBusy ? 'Connecting…' : 'Connect'}
				</Button>
			</div>
		{/if}
	{:else}
		{#each service.fields as f (f.key)}
			<SecretField
				service={service.service}
				fieldKey={f.key}
				label={f.label}
				type={f.type}
				configured={service.configured_keys.includes(f.key)}
				onSaved={onChanged}
				onDeleted={onChanged}
			/>
		{/each}
	{/if}
</section>

<style>
	/* Inherit shared .settings .card / .section-header / .status-pill /
	   .meta / .empty styling — this component is meant to be used inside
	   <div class="settings"> wrappers that already pull in settings.css. */
	.used-by {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.used-by code {
		background: var(--surface-raised);
		padding: 0 0.3rem;
		border-radius: 0.2rem;
		font-size: 0.9em;
		color: var(--text-muted);
	}

	.oauth-actions {
		display: flex;
		gap: 0.5rem;
	}
</style>
