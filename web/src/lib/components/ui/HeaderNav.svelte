<script lang="ts">
	import { goto } from '$app/navigation';
	import NavLink from './NavLink.svelte';

	export interface NavItem {
		href: string;
		label: string;
		active?: boolean;
	}

	interface Props {
		items: NavItem[];
		/** Accessible label for the mobile dropdown. */
		ariaLabel?: string;
	}

	let { items, ariaLabel = 'Section' }: Props = $props();

	// The dropdown must always show a selection (native <select>); reflect the
	// active section, falling back to the first item when none is active
	// (e.g. on a settings sub-page reached via the cog, not the nav).
	const current = $derived(items.find((i) => i.active)?.href ?? items[0]?.href ?? '');

	function onChange(e: Event) {
		const href = (e.currentTarget as HTMLSelectElement).value;
		if (href) goto(href);
	}
</script>

<!-- Desktop: inline links. Under 768px: a dropdown matching the feeds
     published/added sort <select> so link-only headers stay one line on a
     phone instead of wrapping. -->
<div class="nav-links">
	{#each items as item (item.href)}
		<NavLink href={item.href} active={item.active}>{item.label}</NavLink>
	{/each}
</div>
<select class="nav-select" aria-label={ariaLabel} value={current} onchange={onChange}>
	{#each items as item (item.href)}
		<option value={item.href}>{item.label}</option>
	{/each}
</select>

<style>
	.nav-links {
		display: flex;
		gap: var(--chip-gap);
		align-items: center;
		flex-wrap: wrap;
		min-width: 0;
	}

	/* Mirrors feeds' .mode-select (routes/feeds/+layout.svelte). */
	.nav-select {
		display: none;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.2rem 0.4rem;
		border-radius: 0.25rem;
		cursor: pointer;
	}

	@media (max-width: 768px) {
		.nav-links {
			display: none;
		}
		.nav-select {
			display: inline-block;
		}
	}
</style>
