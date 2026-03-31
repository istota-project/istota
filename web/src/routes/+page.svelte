<script lang="ts">
	import { base } from '$app/paths';
	import { getMe, type User } from '$lib/api';
	import { onMount } from 'svelte';

	let user: User | null = $state(null);

	onMount(async () => {
		try {
			user = await getMe();
		} catch {
			// layout handles auth redirect
		}
	});
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
</style>
