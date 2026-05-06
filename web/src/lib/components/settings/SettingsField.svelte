<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		label: string;
		hint?: string;
		error?: string;
		wide?: boolean;
		checkbox?: boolean;
		children: Snippet;
	}

	let {
		label,
		hint,
		error,
		wide = false,
		checkbox = false,
		children,
	}: Props = $props();
</script>

<label class="field" class:field-wide={wide} class:checkbox>
	{#if checkbox}
		{@render children()}
		<span>{label}</span>
	{:else}
		<span>{label}</span>
		{@render children()}
	{/if}
	{#if hint}<small class="field-hint">{hint}</small>{/if}
	{#if error}<small class="field-error">{error}</small>{/if}
</label>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--text-sm);
	}

	.field > span {
		color: var(--text-muted);
	}

	.field :global(input:not([type='checkbox'])),
	.field :global(select),
	.field :global(textarea) {
		background: var(--surface-base);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		padding: 0.3rem 0.5rem;
		font: inherit;
		font-size: var(--text-sm);
		width: 100%;
		max-width: 24rem;
		min-width: 0;
		box-sizing: border-box;
	}

	.field :global(textarea) {
		font-family: var(--font-mono, ui-monospace, SFMono-Regular, monospace);
		resize: vertical;
	}

	.field-wide :global(textarea),
	.field-wide :global(input) {
		max-width: 36rem;
	}

	.field :global(input:focus),
	.field :global(select:focus),
	.field :global(textarea:focus) {
		outline: 1px solid var(--accent, #6c8ebf);
	}

	.field.checkbox {
		flex-direction: row;
		align-items: center;
		gap: 0.4rem;
		color: var(--text-primary);
	}

	.field.checkbox > span {
		color: var(--text-primary);
	}

	.field.checkbox :global(input[type='checkbox']) {
		width: auto;
	}

	.field-hint {
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	.field-error {
		font-size: var(--text-xs);
		color: #e88;
	}
</style>
