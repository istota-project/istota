<script lang="ts">
	interface Props {
		open: boolean;
		label: string;
		count?: number;
		onclick: () => void;
	}

	let { open, label, count, onclick }: Props = $props();
</script>

<button class="sidebar-toggle" class:open {onclick} type="button">
	{open ? 'Close' : label}{#if count !== undefined} ({count}){/if}
</button>

<style>
	.sidebar-toggle {
		display: none;
		background: var(--surface-card);
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.25rem 0.6rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}

	@media (max-width: 768px) {
		.sidebar-toggle {
			display: block;
			position: fixed;
			bottom: 0.75rem;
			left: 0.75rem;
			background: #161616;
			color: var(--text-muted);
			padding: 0.3rem 0.6rem;
			border-radius: 0.25rem;
			font-size: var(--text-xs);
			border: 1px solid var(--border-subtle);
			box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
			z-index: 30;
		}

		.sidebar-toggle:hover {
			color: var(--text-primary);
			background: var(--surface-raised);
		}

		/* When the sidebar is open it covers the bottom-left corner; hide
		   the floating toggle and rely on the backdrop for tap-to-close. */
		.sidebar-toggle.open {
			display: none;
		}
	}
</style>
