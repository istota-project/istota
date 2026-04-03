<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		checked?: boolean;
		icon?: boolean;
		onclick?: () => void;
		children?: Snippet;
		title?: string;
	}

	let { checked = false, icon = false, onclick, children, title, ...rest }: Props = $props();
</script>

<button
	class="chip"
	class:checked
	class:icon
	{onclick}
	{title}
	type="button"
	{...rest}
>{#if children}{@render children()}{/if}</button>

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

	.chip.icon {
		padding: 0.3rem;
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
		.chip.icon { padding: 0.25rem; }
	}
</style>
