<script lang="ts">
	import { ChevronRight } from 'lucide-svelte';

	interface Props {
		open: boolean;
		label: string;
		count?: number;
		onclick: () => void;
	}

	let { open, label, count, onclick }: Props = $props();
</script>

<button
	class="sidebar-toggle"
	class:open
	{onclick}
	type="button"
	aria-label={open ? `Close ${label}` : `Open ${label}${count !== undefined ? ` (${count})` : ''}`}
	title={open ? `Close ${label}` : `${label}${count !== undefined ? ` (${count})` : ''}`}
>
	<ChevronRight size={16} />
</button>

<style>
	.sidebar-toggle {
		display: none;
		background: var(--surface-card);
		border: none;
		color: var(--text-muted);
		font: inherit;
		cursor: pointer;
		align-items: center;
		justify-content: center;
	}

	@media (max-width: 768px) {
		.sidebar-toggle {
			display: inline-flex;
			position: fixed;
			top: 50%;
			left: 0;
			transform: translateY(-50%);
			padding: 0.5rem 0.3rem;
			background: #161616;
			color: var(--text-muted);
			border: 1px solid var(--border-subtle);
			border-left: none;
			border-radius: 0 var(--radius-card) var(--radius-card) 0;
			box-shadow: 0 2px 6px rgba(0, 0, 0, 0.35);
			z-index: 30;
		}

		.sidebar-toggle:hover {
			color: var(--text-primary);
			background: var(--surface-raised);
		}

		/* Sidebar covers the left edge when open; hide the tab and rely on
		   the transparent click-outside backdrop for dismiss. */
		.sidebar-toggle.open {
			display: none;
		}
	}
</style>
