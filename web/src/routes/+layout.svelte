<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { getMe, AuthError, type User } from '$lib/api';
	import '../app.css';

	let { children } = $props();

	let user: User | null = $state(null);
	let loading = $state(true);
	let error = $state('');

	onMount(async () => {
		try {
			user = await getMe();
		} catch (e) {
			if (e instanceof AuthError) {
				window.location.href = `${base}/login`;
				return;
			}
			error = 'Failed to load user info';
		} finally {
			loading = false;
		}
	});

	function isActive(path: string): boolean {
		const current = page.url.pathname;
		if (path === '/') return current === `${base}` || current === `${base}/`;
		return current.startsWith(`${base}${path}`);
	}

	const pageTitle = $derived.by(() => {
		const path = page.url.pathname.replace(base, '').replace(/^\/+/, '');
		if (!path) return 'Istota';
		const segment = path.split('/')[0];
		return `Istota - ${segment.charAt(0).toUpperCase()}${segment.slice(1)}`;
	});
</script>

<svelte:head>
	<title>{pageTitle}</title>
</svelte:head>

{#if loading}
	<div class="loading">Loading...</div>
{:else if error}
	<div class="error-msg">{error}</div>
{:else if user}
	<nav class="app-nav">
		<a href="{base}/" class="app-name">Istota</a>
		<div class="nav-links">
			{#if user.features.feeds}
				<a href="{base}/feeds" class:active={isActive('/feeds')}>Feeds</a>
			{/if}
			{#if user.features.location}
				<a href="{base}/location" class:active={isActive('/location')}>Location</a>
			{/if}
		</div>
		<div class="nav-right">
			<span>{user.display_name}</span>
			<a href="{base}/logout">log out</a>
		</div>
	</nav>
	<main class="app-content" class:app-content-fill={isActive('/location') || isActive('/feeds')}>
		{@render children()}
	</main>
{/if}
