<script lang="ts">
	import { Loader, Check, X, ChevronRight, ChevronDown } from 'lucide-svelte';
	import type { ToolEntry } from '$lib/stores/chat';

	let { tools }: { tools: ToolEntry[] } = $props();

	let expanded = $state(false);

	// The active (currently running) tool drives the minimized view; prefer the
	// most recently started one.
	const active = $derived([...tools].reverse().find((t) => t.running) ?? null);
	const anyRunning = $derived(tools.some((t) => t.running));
	const anyFailed = $derived(tools.some((t) => t.success === false));
	const count = $derived(tools.length);

	function label(t: ToolEntry): string {
		return t.progress || t.description || `Using ${t.name}`;
	}
	const summary = $derived(
		active ? label(active) : `${count} tool call${count === 1 ? '' : 's'}`,
	);
</script>

<div class="tool-strip" class:open={expanded}>
	<button class="head" onclick={() => (expanded = !expanded)} type="button" aria-expanded={expanded}>
		<span class="status">
			{#if anyRunning}
				<Loader size={13} class="spin" />
			{:else if anyFailed}
				<X size={13} />
			{:else}
				<Check size={13} />
			{/if}
		</span>
		<span class="summary">{summary}</span>
		<span class="chev">
			{#if expanded}<ChevronDown size={13} />{:else}<ChevronRight size={13} />{/if}
		</span>
	</button>

	{#if expanded}
		<div class="list">
			{#each tools as tool (tool.id)}
				<div class="row">
					<span class="status">
						{#if tool.running}
							<Loader size={12} class="spin" />
						{:else if tool.success === false}
							<X size={12} />
						{:else}
							<Check size={12} />
						{/if}
					</span>
					<span class="row-text">
						<span class="row-desc">{tool.description || tool.name}</span>
						{#if tool.progress}<span class="row-progress">{tool.progress}</span>{/if}
					</span>
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.tool-strip {
		margin-bottom: 0.45rem;
		border: 1px solid var(--border-subtle);
		border-radius: 0.4rem;
		background: var(--surface-badge);
		max-width: 100%;
		width: fit-content;
		min-width: 0;
	}
	.tool-strip.open { width: 100%; }

	.head {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		width: 100%;
		background: none;
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.3rem 0.5rem;
		cursor: pointer;
		text-align: left;
		min-width: 0;
	}
	.head:hover { color: var(--text-secondary); }

	.status { display: inline-flex; align-items: center; flex: 0 0 auto; }

	.summary {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: ui-monospace, monospace;
	}
	.chev { display: inline-flex; align-items: center; flex: 0 0 auto; opacity: 0.6; }

	.list {
		border-top: 1px solid var(--border-subtle);
		padding: 0.3rem 0.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.row { display: flex; align-items: flex-start; gap: 0.4rem; }
	.row-text { min-width: 0; display: flex; flex-direction: column; gap: 0.1rem; }
	.row-desc {
		font-family: ui-monospace, monospace;
		font-size: var(--text-xs);
		color: var(--text-secondary);
		word-break: break-word;
	}
	.row-progress {
		font-family: ui-monospace, monospace;
		font-size: var(--text-xs);
		color: var(--text-dim);
		white-space: pre-wrap;
		word-break: break-word;
	}

	:global(.tool-strip .spin) { animation: tool-spin 1s linear infinite; }
	@keyframes tool-spin { to { transform: rotate(360deg); } }
</style>
