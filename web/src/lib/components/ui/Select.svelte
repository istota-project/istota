<script lang="ts">
	import { Select as BitsSelect } from 'bits-ui';
	import { ChevronDown } from 'lucide-svelte';

	export interface SelectOption {
		value: string;
		label: string;
	}

	interface Props {
		value: string;
		options: SelectOption[];
		onValueChange?: (value: string) => void;
		placeholder?: string;
		disabled?: boolean;
		ariaLabel?: string;
	}

	let {
		value = $bindable(''),
		options,
		onValueChange,
		placeholder = 'Select…',
		disabled = false,
		ariaLabel,
	}: Props = $props();

	const selectedLabel = $derived(options.find((o) => o.value === value)?.label ?? placeholder);
</script>

<BitsSelect.Root type="single" bind:value {onValueChange} {disabled}>
	<BitsSelect.Trigger class="ui-select-trigger" aria-label={ariaLabel}>
		<span class="ui-select-label">{selectedLabel}</span>
		<ChevronDown size={12} />
	</BitsSelect.Trigger>
	<BitsSelect.Portal>
		<BitsSelect.Content class="ui-select-content" sideOffset={4}>
			<BitsSelect.Viewport class="ui-select-viewport">
				{#each options as opt (opt.value)}
					<BitsSelect.Item value={opt.value} label={opt.label} class="ui-select-item">
						{opt.label}
					</BitsSelect.Item>
				{/each}
			</BitsSelect.Viewport>
		</BitsSelect.Content>
	</BitsSelect.Portal>
</BitsSelect.Root>

<style>
	:global(.ui-select-trigger) {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		background: var(--surface-card);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		padding: 0.15rem 0.5rem;
		font: inherit;
		font-size: var(--text-xs);
		line-height: 1.2;
		cursor: pointer;
		transition: background var(--transition-fast);
	}
	:global(.ui-select-trigger:hover) {
		background: var(--surface-raised);
	}
	:global(.ui-select-trigger:disabled) {
		opacity: 0.5;
		cursor: not-allowed;
	}
	:global(.ui-select-label) {
		max-width: 220px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	:global(.ui-select-content) {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: 0.4rem;
		padding: 0.25rem;
		z-index: 100;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
		min-width: var(--bits-select-anchor-width, 8rem);
		max-height: 18rem;
		overflow: auto;
		outline: none;
	}

	:global(.ui-select-viewport) {
		display: flex;
		flex-direction: column;
		gap: 0.05rem;
	}

	:global(.ui-select-item) {
		padding: 0.3rem 0.5rem;
		font-size: var(--text-sm);
		color: var(--text-secondary);
		border-radius: 0.3rem;
		cursor: pointer;
		outline: none;
		user-select: none;
	}
	:global(.ui-select-item[data-highlighted]) {
		background: var(--surface-raised);
		color: var(--text-primary);
	}
	:global(.ui-select-item[data-selected]) {
		color: var(--text-primary);
	}
</style>
