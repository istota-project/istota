<script lang="ts">
	interface Props {
		checked?: boolean;
		onclick?: () => void;
	}

	let { checked = false, onclick, ...rest }: Props = $props();
</script>

<button
	class="chip"
	class:checked
	{onclick}
	type="button"
	{...rest}
><slot /></button>

<style>
	.chip {
		display: inline-flex;
		align-items: center;
		padding: 0.25rem 0.55rem;
		border: none;
		border-radius: var(--radius-pill);
		font-size: var(--text-xs);
		font-family: inherit;
		transition: all var(--transition-fast);
		user-select: none;
		cursor: pointer;
		color: var(--text-muted);
		background: var(--surface-card);
	}

	.chip:hover {
		color: var(--text-primary);
		background: var(--surface-raised);
	}

	.chip.checked {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	@media (max-width: 640px) {
		.chip { padding: 0.2rem 0.45rem; }
	}
</style>
