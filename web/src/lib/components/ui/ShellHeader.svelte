<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		nav?: Snippet;
		tools?: Snippet;
	}

	let { title, nav, tools }: Props = $props();
</script>

<div class="header">
	<h1>{title}</h1>
	{#if nav}<div class="header-nav">{@render nav()}</div>{/if}
	{#if tools}<div class="header-tools">{@render tools()}</div>{/if}
</div>

<style>
	.header {
		display: flex;
		align-items: baseline;
		gap: 1rem;
		padding: 0.75rem;
		border-bottom: 1px solid var(--border-subtle);
	}

	.header h1 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0;
	}

	.header-nav {
		display: flex;
		gap: var(--chip-gap);
		align-items: baseline;
		flex-wrap: wrap;
		min-width: 0;
	}

	.header-tools {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	@media (max-width: 768px) {
		.header {
			padding: 0.5rem 0.75rem;
			gap: 0.5rem;
		}
	}
</style>
