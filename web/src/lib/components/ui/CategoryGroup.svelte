<script lang="ts">
	import { untrack, type Snippet } from 'svelte';

	interface Props {
		label: string;
		count?: number;
		collapsible?: boolean;
		defaultOpen?: boolean;
		/** When provided, the label becomes a filter button (caret still
		 *  collapses) and the header can render an active/selected state. */
		onSelect?: () => void;
		active?: boolean;
		children: Snippet;
	}

	let {
		label,
		count,
		collapsible = false,
		defaultOpen = true,
		onSelect,
		active = false,
		children,
	}: Props = $props();

	let open = $state(untrack(() => defaultOpen));

	function toggle(e: MouseEvent) {
		e.stopPropagation();
		open = !open;
	}
</script>

<div class="cat-group">
	{#if collapsible && onSelect}
		<!-- Selectable header: caret collapses, label filters to this category. -->
		<div class="cat-header" class:active>
			<button class="caret-btn" onclick={toggle} type="button" aria-label={open ? 'Collapse' : 'Expand'}>
				<span class="caret" class:open>&#9654;</span>
			</button>
			<button class="cat-select" onclick={onSelect} type="button">
				<span class="cat-label-text">{label}</span>
				{#if count !== undefined}<span class="cat-count">{count}</span>{/if}
			</button>
		</div>
		{#if open}
			{@render children()}
		{/if}
	{:else if collapsible}
		<button class="cat-label cat-label-button" onclick={toggle} type="button">
			<span class="caret" class:open>&#9654;</span>
			<span class="cat-label-text">{label}</span>
			{#if count !== undefined}<span class="cat-count">{count}</span>{/if}
		</button>
		{#if open}
			{@render children()}
		{/if}
	{:else}
		<div class="cat-label">
			<span class="cat-label-text">{label}</span>
			{#if count !== undefined}<span class="cat-count">{count}</span>{/if}
		</div>
		{@render children()}
	{/if}
</div>

<style>
	.cat-group {
		margin-bottom: 0.25rem;
	}

	.cat-label {
		display: flex;
		align-items: baseline;
		gap: 0.35rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		padding: 0.35rem 0.75rem 0.15rem;
	}

	.cat-label-button {
		width: 100%;
		background: none;
		border: none;
		font: inherit;
		font-size: var(--text-xs);
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
		cursor: pointer;
		text-align: left;
		transition: color var(--transition-fast);
	}

	.cat-label-button:hover {
		color: var(--text-muted);
	}

	/* Selectable header (onSelect provided): caret + filter-label in one row. */
	.cat-header {
		display: flex;
		align-items: stretch;
		gap: 0.1rem;
		border-radius: 0.3rem;
		transition: background var(--transition-fast);
	}

	.cat-header:hover {
		background: var(--surface-raised);
	}

	.cat-header.active {
		background: var(--surface-raised);
	}

	.caret-btn {
		display: inline-flex;
		align-items: center;
		background: none;
		border: none;
		cursor: pointer;
		padding: 0.35rem 0.15rem 0.15rem 0.6rem;
	}

	.cat-select {
		flex: 1;
		display: flex;
		align-items: baseline;
		gap: 0.35rem;
		background: none;
		border: none;
		font: inherit;
		font-size: var(--text-xs);
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
		cursor: pointer;
		text-align: left;
		padding: 0.35rem 0.75rem 0.15rem 0;
		transition: color var(--transition-fast);
	}

	.cat-header:hover .caret,
	.cat-header:hover .cat-select {
		color: var(--text-muted);
	}

	.cat-header.active .cat-select,
	.cat-header.active .caret {
		color: var(--text-primary);
	}

	.caret {
		font-size: 0.45rem;
		display: inline-block;
		color: var(--text-dim);
		transition: transform var(--transition-fast);
	}

	.caret.open {
		transform: rotate(90deg);
	}

	.cat-count {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: none;
		letter-spacing: 0;
	}
</style>
