<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { AppShell, ShellHeader, NavLink, Chip } from '$lib/components/ui';
	import { Cog } from 'lucide-svelte';

	let { children } = $props();

	function isActive(path: string): boolean {
		return page.url.pathname.startsWith(`${base}${path}`);
	}

	function isExactActive(path: string): boolean {
		const current = page.url.pathname;
		return current === `${base}${path}` || current === `${base}${path}/`;
	}

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
				<NavLink
					href="{base}/health"
					active={isExactActive('/health') || isActive('/health/stats')}
				>Stats</NavLink>
				<NavLink
					href="{base}/health/history"
					active={isActive('/health/history')}
				>History</NavLink>
				<NavLink
					href="{base}/health/bloodwork"
					active={isActive('/health/bloodwork')}
				>Bloodwork</NavLink>
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

	@media (max-width: 768px) {
		.health-frame {
			/* Match ShellHeader's mobile padding so the page heading lines
			   up with the subnav title above it. */
			padding: 0.5rem 0.75rem;
		}
	}
</style>
