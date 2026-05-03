<script lang="ts" module>
	import { getContext, setContext } from 'svelte';

	const SHELL_SCROLL_ROOT = Symbol('shell-scroll-root');

	export function setShellScrollRoot(getter: () => HTMLElement | undefined): void {
		setContext(SHELL_SCROLL_ROOT, getter);
	}

	export function getShellScrollRoot(): (() => HTMLElement | undefined) | undefined {
		return getContext<() => HTMLElement | undefined>(SHELL_SCROLL_ROOT);
	}
</script>

<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		header: Snippet;
		sidebar?: Snippet;
		children: Snippet;
		extras?: Snippet;
	}

	let { header, sidebar, children, extras }: Props = $props();

	let mainEl: HTMLDivElement | undefined = $state();
	setShellScrollRoot(() => mainEl);
</script>

<div class="shell">
	<div class="shell-header">{@render header()}</div>
	{#if extras}{@render extras()}{/if}
	<div class="shell-body">
		{#if sidebar}{@render sidebar()}{/if}
		<div class="shell-main" bind:this={mainEl}>{@render children()}</div>
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
		overflow-y: auto;
	}

	.shell-main::-webkit-scrollbar { width: 4px; }
	.shell-main::-webkit-scrollbar-track { background: transparent; }
	.shell-main::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	@media (max-width: 768px) {
		.shell {
			margin: -1rem -0.75rem;
			height: calc(100vh - 36px);
		}
	}
</style>
