<script lang="ts">
	import type { Snippet } from 'svelte';
	import { Dialog } from 'bits-ui';

	interface Props {
		open: boolean;
		title: string;
		description?: string;
		onOpenChange?: (open: boolean) => void;
		children: Snippet;
		footer?: Snippet;
		width?: string;
	}

	let {
		open = $bindable(false),
		title,
		description,
		onOpenChange,
		children,
		footer,
		width = '420px',
	}: Props = $props();
</script>

<Dialog.Root bind:open {onOpenChange}>
	<Dialog.Portal>
		<Dialog.Overlay class="ui-modal-overlay" />
		<Dialog.Content class="ui-modal-content" style="--modal-width: {width}">
			<Dialog.Title class="ui-modal-title">{title}</Dialog.Title>
			{#if description}
				<Dialog.Description class="ui-modal-description">{description}</Dialog.Description>
			{/if}
			<div class="ui-modal-body">{@render children()}</div>
			{#if footer}
				<div class="ui-modal-footer">{@render footer()}</div>
			{/if}
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>

<style>
	:global(.ui-modal-overlay) {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.6);
		z-index: 50;
	}
	:global(.ui-modal-content) {
		position: fixed;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 1rem;
		width: var(--modal-width, 420px);
		max-width: calc(100vw - 2rem);
		max-height: calc(100vh - 2rem);
		overflow: auto;
		z-index: 51;
		outline: none;
	}
	:global(.ui-modal-title) {
		font-size: var(--text-base);
		font-weight: 600;
		margin: 0 0 0.5rem;
		color: var(--text-primary);
	}
	:global(.ui-modal-description) {
		font-size: var(--text-sm);
		color: var(--text-muted);
		margin: 0 0 0.75rem;
	}
	:global(.ui-modal-footer) {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 1rem;
		padding-top: 0.75rem;
		border-top: 1px solid var(--border-subtle);
	}
</style>
