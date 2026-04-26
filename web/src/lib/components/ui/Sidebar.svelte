<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		count?: number;
		open?: boolean;
		width?: string;
		onClose?: () => void;
		extras?: Snippet;
		children: Snippet;
	}

	let { title, count, open = false, width = '220px', onClose, extras, children }: Props = $props();
</script>

{#if open && onClose}
	<button
		class="sidebar-backdrop"
		aria-label="Close sidebar"
		onclick={onClose}
		type="button"
	></button>
{/if}

<aside class="sidebar" class:open style="--sidebar-width: {width}">
	<div class="sidebar-header">
		<span class="sidebar-title">{title}</span>
		{#if count !== undefined}<span class="sidebar-count">{count}</span>{/if}
	</div>
	{#if extras}{@render extras()}{/if}
	<div class="sidebar-list">{@render children()}</div>
</aside>

<style>
	.sidebar {
		width: var(--sidebar-width);
		flex-shrink: 0;
		border-right: 1px solid var(--border-subtle);
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.sidebar-header {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		padding: 0.75rem;
		flex-shrink: 0;
	}

	.sidebar-title {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.sidebar-count {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.sidebar-list {
		flex: 1;
		min-width: 0;
		overflow-x: hidden;
		overflow-y: auto;
		padding: 0 0.25rem 0.5rem;
	}

	.sidebar-list::-webkit-scrollbar { width: 4px; }
	.sidebar-list::-webkit-scrollbar-track { background: transparent; }
	.sidebar-list::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.sidebar-backdrop {
		display: none;
	}

	@media (max-width: 768px) {
		.sidebar {
			display: none;
			position: absolute;
			top: 0;
			left: 0;
			bottom: 0;
			z-index: 20;
			width: 220px;
			background: var(--surface-base);
			border-right: 1px solid var(--border-default);
		}

		.sidebar.open {
			display: flex;
		}

		.sidebar-backdrop {
			display: block;
			position: fixed;
			inset: 0;
			background: transparent;
			border: none;
			padding: 0;
			cursor: default;
			z-index: 19;
		}
	}
</style>
