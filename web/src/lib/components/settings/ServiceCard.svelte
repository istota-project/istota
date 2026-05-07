<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { setSecret, deleteSecret, type ServiceCard as ServiceCardData } from '$lib/api';
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

	let pending: Record<string, string> = $state({});
	let saving = $state(false);
	let savedFlash = $state(false);
	let saveError = $state('');
	let confirmingClearKey: string | null = $state(null);

	let dirty = $derived(
		Object.values(pending).some((v) => v && v.length > 0),
	);

	function setFieldValue(key: string, next: string) {
		pending = { ...pending, [key]: next };
	}

	async function saveAll() {
		const entries = Object.entries(pending).filter(([, v]) => v && v.length > 0);
		if (entries.length === 0) return;
		saving = true;
		saveError = '';
		try {
			for (const [key, value] of entries) {
				await setSecret(service.service, key, value);
			}
			pending = {};
			savedFlash = true;
			setTimeout(() => {
				savedFlash = false;
			}, 1500);
			onChanged?.();
		} catch (e) {
			saveError = e instanceof Error ? e.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	async function performClear(key: string) {
		confirmingClearKey = null;
		saving = true;
		saveError = '';
		try {
			await deleteSecret(service.service, key);
			// Drop any pending edit for the cleared key.
			if (key in pending) {
				const { [key]: _drop, ...rest } = pending;
				pending = rest;
			}
			onChanged?.();
		} catch (e) {
			saveError = e instanceof Error ? e.message : 'Delete failed';
		} finally {
			saving = false;
		}
	}
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
		<div class="header-actions">
			{#if service.last_updated}
				<span class="meta">Updated {service.last_updated}</span>
			{/if}
			{#if !service.oauth && service.fields.length > 0}
				{#if dirty}
					<span class="dirty-badge">Unsaved changes</span>
				{/if}
				{#if savedFlash}
					<span class="saved-flash">Saved.</span>
				{/if}
				<Button
					variant="primary"
					size="sm"
					disabled={!dirty || saving}
					onclick={saveAll}
				>
					{saving ? 'Saving…' : 'Save'}
				</Button>
			{/if}
		</div>
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
				label={f.label}
				type={f.type}
				configured={service.configured_keys.includes(f.key)}
				value={pending[f.key] ?? ''}
				disabled={saving}
				onValueChange={(v) => setFieldValue(f.key, v)}
				onRequestClear={() => (confirmingClearKey = f.key)}
			/>
			{#if confirmingClearKey === f.key}
				<div class="clear-confirm">
					<span>Clear stored <code>{f.label}</code>?</span>
					<Button variant="ghost" size="sm" onclick={() => (confirmingClearKey = null)}>
						Cancel
					</Button>
					<Button variant="primary" size="sm" onclick={() => performClear(f.key)}>
						Clear
					</Button>
				</div>
			{/if}
		{/each}
		{#if saveError}
			<div class="banner error">{saveError}</div>
		{/if}
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

	.saved-flash {
		font-size: var(--text-xs);
		color: #6eb884;
	}

	.clear-confirm {
		display: flex;
		gap: 0.4rem;
		align-items: center;
		font-size: var(--text-xs);
		color: var(--text-muted);
	}
</style>
