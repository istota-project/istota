<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getSettingsServices,
		setSecret,
		deleteSecret,
		type ServiceCard,
	} from '$lib/api';

	let services: ServiceCard[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// Per-(service, key) form state, keyed as `${service}:${key}`. Plaintext
	// inputs never round-trip through the server — they're cleared after a
	// successful save.
	let inputs: Record<string, string> = $state({});
	let saving: Record<string, boolean> = $state({});
	let savedFlash: Record<string, boolean> = $state({});
	let saveError: Record<string, string> = $state({});

	function fieldId(service: string, key: string): string {
		return `${service}:${key}`;
	}

	async function refresh() {
		loading = true;
		try {
			const resp = await getSettingsServices();
			services = resp.services;
			error = '';
		} catch (e) {
			error = (e as Error).message || 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	async function save(service: string, key: string) {
		const id = fieldId(service, key);
		const value = inputs[id] ?? '';
		saving[id] = true;
		saveError[id] = '';
		try {
			await setSecret(service, key, value);
			inputs[id] = '';
			savedFlash[id] = true;
			setTimeout(() => {
				savedFlash[id] = false;
			}, 1500);
			await refresh();
		} catch (e) {
			saveError[id] = (e as Error).message || 'Save failed';
		} finally {
			saving[id] = false;
		}
	}

	async function clear(service: string, key: string) {
		if (!confirm(`Clear ${service}/${key}?`)) return;
		const id = fieldId(service, key);
		saving[id] = true;
		saveError[id] = '';
		try {
			await deleteSecret(service, key);
			await refresh();
		} catch (e) {
			saveError[id] = (e as Error).message || 'Delete failed';
		} finally {
			saving[id] = false;
		}
	}

	onMount(() => {
		void refresh();
	});

	function statusLabel(s: ServiceCard['status']): string {
		switch (s) {
			case 'configured':
				return 'Configured';
			case 'partial':
				return 'Partial';
			case 'missing':
				return 'Missing';
			case 'unavailable':
				return 'Not enabled';
		}
	}
</script>

<div class="settings">
	<h1>Settings</h1>
	<p class="lead">
		Per-service credentials for the integrations your account has enabled.
		Values are encrypted at rest and never sent back to the browser — the
		fields below are write-only.
	</p>

	{#if loading}
		<div class="muted">Loading…</div>
	{:else if error}
		<div class="error">{error}</div>
	{:else}
		<div class="cards">
			{#each services as svc (svc.service)}
				<section class="card" data-status={svc.status}>
					<header>
						<div class="title">
							<h2>{svc.label}</h2>
							<span class="status-pill status-{svc.status}">
								{statusLabel(svc.status)}
							</span>
						</div>
						{#if svc.last_updated}
							<div class="meta">Last updated: {svc.last_updated}</div>
						{/if}
					</header>

					{#if svc.status === 'unavailable'}
						<p class="muted">
							This service is not declared as a resource for your account.
							Add a <code>[[users.X.resources]]</code> entry in
							<code>config.toml</code> to enable it.
						</p>
					{:else}
						<div class="fields">
							{#each svc.fields as field (field.key)}
								{@const id = fieldId(svc.service, field.key)}
								{@const isConfigured = svc.configured_keys.includes(field.key)}
								<div class="field">
									<label for="input-{id}">{field.label}</label>
									<div class="row">
										<input
											id="input-{id}"
											type={field.type}
											autocomplete="new-password"
											placeholder={isConfigured ? '•••• stored — enter to replace' : 'Enter value'}
											bind:value={inputs[id]}
											disabled={saving[id]}
										/>
										<button
											type="button"
											class="btn btn-primary"
											disabled={saving[id] || !inputs[id]}
											onclick={() => save(svc.service, field.key)}
										>
											{saving[id] ? 'Saving…' : 'Save'}
										</button>
										{#if isConfigured}
											<button
												type="button"
												class="btn btn-danger"
												disabled={saving[id]}
												onclick={() => clear(svc.service, field.key)}
											>
												Clear
											</button>
										{/if}
									</div>
									{#if savedFlash[id]}
										<div class="flash">Saved.</div>
									{/if}
									{#if saveError[id]}
										<div class="error">{saveError[id]}</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</section>
			{/each}
		</div>
	{/if}
</div>

<style>
	.settings {
		max-width: 48rem;
		margin: 0 auto;
		padding: 1.25rem;
	}

	h1 {
		font-size: var(--text-2xl, 1.5rem);
		margin: 0 0 0.5rem 0;
	}

	.lead {
		color: var(--text-muted);
		margin: 0 0 1.5rem 0;
	}

	.cards {
		display: grid;
		gap: 1rem;
	}

	.card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 1rem 1.1rem;
	}

	.card header {
		margin-bottom: 0.75rem;
	}

	.title {
		display: flex;
		align-items: center;
		gap: 0.6rem;
	}

	.title h2 {
		margin: 0;
		font-size: var(--text-lg, 1.05rem);
	}

	.status-pill {
		font-size: var(--text-xs, 0.75rem);
		padding: 0.1rem 0.5rem;
		border-radius: var(--radius-pill);
		background: var(--surface-raised);
		color: var(--text-muted);
	}

	.status-pill.status-configured { color: #3a8a3a; }
	.status-pill.status-partial { color: #b88a2a; }
	.status-pill.status-missing { color: var(--text-dim); }
	.status-pill.status-unavailable { color: var(--text-dim); }

	.meta {
		font-size: var(--text-xs, 0.75rem);
		color: var(--text-dim);
		margin-top: 0.25rem;
	}

	.fields {
		display: grid;
		gap: 0.85rem;
	}

	.field {
		display: grid;
		gap: 0.3rem;
	}

	label {
		font-size: var(--text-sm, 0.85rem);
		color: var(--text-muted);
	}

	.row {
		display: flex;
		gap: 0.4rem;
		align-items: center;
	}

	input {
		flex: 1;
		font: inherit;
		padding: 0.35rem 0.55rem;
		border-radius: var(--radius-card);
		border: 1px solid var(--border-default);
		background: var(--surface-base);
		color: var(--text-primary);
	}

	input:focus {
		outline: none;
		border-color: var(--accent);
	}

	.btn {
		font: inherit;
		padding: 0.35rem 0.7rem;
		border-radius: var(--radius-pill);
		border: none;
		cursor: pointer;
	}

	.btn-primary {
		background: var(--accent);
		color: var(--surface-base);
	}

	.btn-primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-danger {
		background: transparent;
		color: var(--text-dim);
	}

	.btn-danger:hover {
		color: #c66;
	}

	.flash {
		font-size: var(--text-xs, 0.75rem);
		color: #3a8a3a;
	}

	.error {
		font-size: var(--text-sm, 0.85rem);
		color: #c66;
	}

	.muted {
		color: var(--text-muted);
	}

	code {
		font-family: var(--font-mono, monospace);
		font-size: 0.9em;
		padding: 0 0.2rem;
		background: var(--surface-raised);
		border-radius: 3px;
	}
</style>
