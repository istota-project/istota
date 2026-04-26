<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		href: string;
		active?: boolean;
		children: Snippet;
	}

	let { href, active = false, children }: Props = $props();
</script>

<a {href} class="nav-link" class:active>{@render children()}</a>

<style>
	.nav-link {
		display: inline-flex;
		align-items: center;
		font-size: var(--text-sm);
		line-height: 1.2;
		color: var(--text-muted);
		text-decoration: none;
		padding: 0.15rem var(--chip-padding-x);
		border-radius: var(--radius-pill);
		transition: all var(--transition-fast);
	}

	.nav-link:hover {
		color: var(--text-primary);
	}

	.nav-link.active {
		background: var(--surface-raised);
		color: var(--text-primary);
	}
</style>
