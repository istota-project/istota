<script lang="ts">
	import type { Snippet } from 'svelte';

	type Variant = 'primary' | 'secondary' | 'ghost' | 'pill' | 'subtle' | 'danger-icon';
	type Size = 'sm' | 'md';

	interface Props {
		variant?: Variant;
		size?: Size;
		type?: 'button' | 'submit' | 'reset';
		onclick?: (e: MouseEvent) => void;
		title?: string;
		disabled?: boolean;
		ariaLabel?: string;
		children: Snippet;
	}

	let {
		variant = 'pill',
		size = 'md',
		type = 'button',
		onclick,
		title,
		disabled,
		ariaLabel,
		children,
	}: Props = $props();
</script>

<button
	class="btn btn-{variant} btn-{size}"
	{type}
	{onclick}
	{title}
	{disabled}
	aria-label={ariaLabel}
>
	{@render children()}
</button>

<style>
	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.35rem;
		border: none;
		font: inherit;
		font-size: var(--text-sm);
		line-height: 1.2;
		border-radius: var(--radius-pill);
		cursor: pointer;
		transition: all var(--transition-fast);
		user-select: none;
	}

	.btn:disabled { opacity: 0.5; cursor: not-allowed; }

	.btn-sm { padding: 0.15rem 0.5rem; font-size: var(--text-xs); }
	.btn-md { padding: 0.25rem 0.6rem; }

	.btn-primary {
		background: var(--accent);
		color: var(--surface-base);
	}
	.btn-primary:hover:not(:disabled) {
		background: var(--accent-hover);
	}

	/* Same filled shape as primary, quieter colour — for the reversing half of
	   a pair (Disconnect next to Connect), where a ghost button would read as a
	   different kind of control rather than a calmer one. Sits on --surface-card
	   surfaces, so it can't use btn-pill's matching background. */
	.btn-secondary {
		background: var(--surface-raised);
		color: var(--text-muted);
	}
	.btn-secondary:hover:not(:disabled) {
		background: var(--border-default);
		color: var(--text-primary);
	}

	.btn-pill {
		background: var(--surface-card);
		color: var(--text-muted);
	}
	.btn-pill:hover:not(:disabled) {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.btn-ghost {
		background: transparent;
		color: var(--text-muted);
	}
	.btn-ghost:hover:not(:disabled) {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.btn-subtle {
		background: transparent;
		color: var(--text-dim);
		padding-inline: 0.35rem;
	}
	.btn-subtle:hover:not(:disabled) {
		color: var(--text-muted);
	}

	.btn-danger-icon {
		background: transparent;
		color: var(--text-dim);
		padding: 0.2rem 0.35rem;
	}
	.btn-danger-icon:hover:not(:disabled) {
		color: #c66;
	}

	/* Light theme overrides — dark rules above untouched. */
	:global(:root[data-theme='light']) .btn-danger-icon:hover:not(:disabled) { color: #c0271d; }
</style>
