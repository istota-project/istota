<script lang="ts">
	import { Loader, Check, Wrench, X } from 'lucide-svelte';
	import type { ToolEntry } from '$lib/stores/chat';

	let { tool }: { tool: ToolEntry } = $props();
	let expanded = $state(false);
</script>

<button
	class="tool-chip"
	class:running={tool.running}
	onclick={() => (expanded = !expanded)}
	type="button"
	title={tool.description || tool.name}
>
	{#if tool.running}
		<Loader size={12} class="spin" />
	{:else if tool.success === false}
		<X size={12} />
	{:else if tool.success}
		<Check size={12} />
	{:else}
		<Wrench size={12} />
	{/if}
	<span class="tool-name">{tool.name}</span>
</button>
{#if expanded && tool.description}
	<div class="tool-detail">{tool.description}</div>
{/if}

<style>
	.tool-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		background: var(--surface-badge);
		border: 1px solid var(--border-subtle);
		color: var(--text-muted);
		font-size: var(--text-xs);
		padding: 0.15rem 0.45rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
		transition: color var(--transition-fast), border-color var(--transition-fast);
	}
	.tool-chip:hover { color: var(--text-secondary); border-color: var(--border-default); }
	.tool-chip.running { color: var(--text-secondary); }
	.tool-name { font-family: ui-monospace, monospace; }
	.tool-detail {
		font-family: ui-monospace, monospace;
		font-size: var(--text-xs);
		color: var(--text-dim);
		background: var(--surface-base);
		border: 1px solid var(--border-subtle);
		border-radius: 0.3rem;
		padding: 0.3rem 0.5rem;
		margin-top: 0.25rem;
		white-space: pre-wrap;
		word-break: break-word;
	}
	:global(.tool-chip .spin) { animation: tool-spin 1s linear infinite; }
	@keyframes tool-spin { to { transform: rotate(360deg); } }
</style>
