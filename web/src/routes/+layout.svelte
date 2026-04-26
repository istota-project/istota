<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { LogOut, Menu } from 'lucide-svelte';
	import { DropdownMenu } from 'bits-ui';
	import { getMe, AuthError, type User } from '$lib/api';
	import '../app.css';

	let { children } = $props();

	let user: User | null = $state(null);
	let loading = $state(true);
	let error = $state('');

	onMount(async () => {
		console.log(`[istota] web ui ${__APP_VERSION__} (built ${__APP_BUILT_AT__})`);
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
			{#if user.features.money}
				<a href="{base}/money" class:active={isActive('/money')}>Money</a>
			{/if}
		</div>
		<div class="nav-right">
			<span class="nav-user">{user.display_name}</span>
			<a href="{base}/logout" class="logout-btn" title="Log out" aria-label="Log out">
				<LogOut size={14} />
			</a>
			<DropdownMenu.Root>
				<DropdownMenu.Trigger>
					{#snippet child({ props })}
						<button class="hamburger-btn" aria-label="Open menu" {...props}>
							<Menu size={18} />
						</button>
					{/snippet}
				</DropdownMenu.Trigger>
				<DropdownMenu.Portal>
					<DropdownMenu.Content class="app-nav-menu" align="end" sideOffset={6}>
						{#if user.features.feeds}
							<DropdownMenu.Item>
								{#snippet child({ props })}
									<a
										href="{base}/feeds"
										class="app-nav-menu-link"
										class:active={isActive('/feeds')}
										{...props}>Feeds</a
									>
								{/snippet}
							</DropdownMenu.Item>
						{/if}
						{#if user.features.location}
							<DropdownMenu.Item>
								{#snippet child({ props })}
									<a
										href="{base}/location"
										class="app-nav-menu-link"
										class:active={isActive('/location')}
										{...props}>Location</a
									>
								{/snippet}
							</DropdownMenu.Item>
						{/if}
						{#if user.features.money}
							<DropdownMenu.Item>
								{#snippet child({ props })}
									<a
										href="{base}/money"
										class="app-nav-menu-link"
										class:active={isActive('/money')}
										{...props}>Money</a
									>
								{/snippet}
							</DropdownMenu.Item>
						{/if}
					</DropdownMenu.Content>
				</DropdownMenu.Portal>
			</DropdownMenu.Root>
		</div>
	</nav>
	<main class="app-content" class:app-content-fill={isActive('/location') || isActive('/feeds') || isActive('/money')}>
		{@render children()}
	</main>
{/if}

<style>
	.hamburger-btn {
		display: none;
		background: none;
		border: none;
		color: var(--text-muted);
		padding: 0.25rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
		align-items: center;
		justify-content: center;
		transition: color var(--transition-fast), background var(--transition-fast);
	}

	.hamburger-btn:hover,
	.hamburger-btn[data-state='open'] {
		color: var(--text-primary);
		background: var(--surface-raised);
	}

	@media (max-width: 640px) {
		.hamburger-btn {
			display: inline-flex;
		}
		.nav-user {
			display: none;
		}
	}

	:global(.app-nav-menu) {
		min-width: 9rem;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.25rem;
		z-index: 60;
		box-shadow: 0 6px 20px rgba(0, 0, 0, 0.4);
		outline: none;
	}

	:global(.app-nav-menu-link) {
		display: block;
		padding: 0.4rem 0.75rem;
		font-size: var(--text-base);
		color: var(--text-muted);
		text-decoration: none;
		border-radius: 0.3rem;
		outline: none;
	}

	:global(.app-nav-menu-link:hover),
	:global(.app-nav-menu-link[data-highlighted]) {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	:global(.app-nav-menu-link.active) {
		color: var(--text-primary);
	}
</style>
