<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		header: Snippet;
		sidebar?: Snippet;
		children: Snippet;
		extras?: Snippet;
	}

	let { header, sidebar, children, extras }: Props = $props();
</script>

<div class="shell">
	<div class="shell-header">{@render header()}</div>
	{#if extras}{@render extras()}{/if}
	<div class="shell-body">
		{#if sidebar}{@render sidebar()}{/if}
		<div class="shell-main">{@render children()}</div>
	</div>
</div>

<style>
	.shell {
		display: flex;
		flex-direction: column;
		margin: -1.5rem;
		height: calc(100vh - 42px);
		overflow: hidden;
	}

	.shell-header {
		flex-shrink: 0;
	}

	.shell-body {
		display: flex;
		flex: 1;
		min-height: 0;
		position: relative;
	}

	.shell-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	@media (max-width: 768px) {
		.shell {
			margin: -1rem -0.75rem;
			height: calc(100vh - 36px);
		}
	}
</style>
