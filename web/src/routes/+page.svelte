<script lang="ts">
	import { base } from '$app/paths';
	import { getMe, disconnectGoogle, type User } from '$lib/api';
	import { onMount } from 'svelte';

	let user: User | null = $state(null);
	let disconnecting = $state(false);

	onMount(async () => {
		try {
			user = await getMe();
		} catch {
			// layout handles auth redirect
		}
	});

	async function handleDisconnectGoogle() {
		if (!confirm('Disconnect your Google account?')) return;
		disconnecting = true;
		try {
			await disconnectGoogle();
			if (user) user.features.google_workspace = false;
		} catch {
			// ignore
		}
		disconnecting = false;
	}
</script>

<div class="dashboard">
	{#if user}
		<h1>Dashboard</h1>
		<div class="feature-grid">
			{#if user.features.feeds}
				<a href="{base}/feeds" class="feature-card">
					<div class="feature-title">Feeds</div>
					<div class="feature-desc">RSS feed reader</div>
				</a>
			{/if}
			{#if user.features.location}
				<a href="{base}/location" class="feature-card">
					<div class="feature-title">Location</div>
					<div class="feature-desc">GPS tracking and map</div>
				</a>
			{/if}
			{#if user.features.google_workspace}
				<div class="feature-card">
					<div class="feature-title">Google Workspace</div>
					<div class="feature-desc">Connected</div>
					<button
						class="disconnect-btn"
						onclick={handleDisconnectGoogle}
						disabled={disconnecting}
					>
						{disconnecting ? 'Disconnecting...' : 'Disconnect'}
					</button>
				</div>
			{:else if user.features.google_workspace_enabled}
				<a href="/istota/google/connect" class="feature-card">
					<div class="feature-title">Google Workspace</div>
					<div class="feature-desc">Connect your Google account</div>
				</a>
			{/if}
		</div>
	{/if}
</div>

<style>
	.dashboard h1 {
		font-size: 1.1rem;
		font-weight: 600;
		margin: 0 0 1.5rem;
	}
	.feature-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
		gap: 1rem;
	}
	.feature-card {
		display: block;
		background: #1a1a1a;
		border-radius: 0.5rem;
		padding: 1.25rem;
		text-decoration: none;
		transition: background 0.15s;
	}
	.feature-card:hover { background: #222; }
	.feature-title {
		font-weight: 600;
		font-size: 0.9rem;
		margin-bottom: 0.25rem;
	}
	.feature-desc {
		font-size: 0.8rem;
		color: #888;
	}
	.disconnect-btn {
		margin-top: 0.75rem;
		padding: 0.25rem 0.75rem;
		font-size: 0.75rem;
		background: transparent;
		border: 1px solid #444;
		border-radius: 999px;
		color: #888;
		cursor: pointer;
		transition: all 0.15s;
	}
	.disconnect-btn:hover { border-color: #888; color: #e0e0e0; }
	.disconnect-btn:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
