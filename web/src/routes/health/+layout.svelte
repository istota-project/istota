<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { AppShell, ShellHeader, HeaderNav, Chip } from '$lib/components/ui';
	import { Cog } from 'lucide-svelte';

	let { children } = $props();

	function isActive(path: string): boolean {
		return page.url.pathname.startsWith(`${base}${path}`);
	}

	function isExactActive(path: string): boolean {
		const current = page.url.pathname;
		return current === `${base}${path}` || current === `${base}${path}/`;
	}

	const navItems = $derived([
		{ href: `${base}/health`, label: 'Stats', active: isExactActive('/health') || isActive('/health/stats') },
		{ href: `${base}/health/history`, label: 'History', active: isActive('/health/history') },
		{ href: `${base}/health/immunizations`, label: 'Immunizations', active: isActive('/health/immunizations') },
		{ href: `${base}/health/bloodwork`, label: 'Bloodwork', active: isActive('/health/bloodwork') },
	]);

	const onSettings = $derived(page.url.pathname.startsWith(`${base}/health/settings`));

	function toggleSettings() {
		if (onSettings) goto(`${base}/health`);
		else goto(`${base}/health/settings`);
	}
</script>

<AppShell>
	{#snippet header()}
		<ShellHeader title="Health">
			{#snippet nav()}
				<HeaderNav items={navItems} ariaLabel="Health section" />
			{/snippet}
			{#snippet tools()}
				<Chip icon checked={onSettings} onclick={toggleSettings} title="Health settings">
					<Cog size={14} />
				</Chip>
			{/snippet}
		</ShellHeader>
	{/snippet}

	<div class="health-frame">
		{@render children()}
	</div>
</AppShell>

<style>
	.health-frame {
		max-width: 1280px;
		margin: 0 auto;
		padding: 1rem;
		width: 100%;
		box-sizing: border-box;
	}

	/* Shared card surface for every health page — the module's counterpart to
	   the global .card-grid layout primitive (app.css). Scoped to .health-frame
	   (not global) because `.card` means other things elsewhere in the app;
	   this mirrors how settings.css scopes `.settings .card`. Pages set their
	   own padding/layout on `.card`; this owns surface + border + radius. Add
	   `class="card interactive"` for a clickable card (cursor + hover border). */
	.health-frame :global(.card) {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: var(--card-padding, 0.75rem 0.9rem);
		box-sizing: border-box;
		min-width: 0;
		text-decoration: none;
	}
	.health-frame :global(.card.interactive) {
		cursor: pointer;
		color: var(--text-primary);
		transition: border-color var(--transition-fast);
	}
	.health-frame :global(.card.interactive:hover) {
		border-color: #555;
	}

	@media (max-width: 768px) {
		.health-frame {
			/* Match ShellHeader's mobile padding so the page heading lines
			   up with the subnav title above it. */
			padding: 0.5rem 0.75rem;
		}
	}
</style>
