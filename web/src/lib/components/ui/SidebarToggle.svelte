<script lang="ts">
	interface Props {
		open: boolean;
		label: string;
		count?: number;
		onclick: () => void;
	}

	let { open, label, count, onclick }: Props = $props();
</script>

<button class="sidebar-toggle" {onclick} type="button">
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
		.sidebar-toggle { display: block; }
	}
</style>
