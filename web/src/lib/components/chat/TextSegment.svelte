<script lang="ts">
	import { ChevronRight, ChevronDown } from 'lucide-svelte';
	import { renderMarkdown } from '$lib/markdown';

	// One text segment of an assistant turn.
	//  - settled = false → the answer (terminal) or the open/streaming block:
	//    rendered as prominent markdown, always expanded.
	//  - settled = true  → it was narration (a tool followed it): rendered as a
	//    dim, collapsed disclosure — a one-line preview with an expand toggle.
	let { text, settled }: { text: string; settled: boolean } = $props();

	let expanded = $state(false);

	const html = $derived(renderMarkdown(text));
	// Collapsed preview: the first non-empty line, trimmed.
	const preview = $derived(
		text.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '',
	);
</script>

{#if !settled}
	<div class="body markdown">{@html html}</div>
{:else}
	<div class="narration" class:open={expanded}>
		<button class="head" onclick={() => (expanded = !expanded)} type="button" aria-expanded={expanded}>
			<span class="chev">
				{#if expanded}<ChevronDown size={12} />{:else}<ChevronRight size={12} />{/if}
			</span>
			{#if expanded}
				<span class="label">Reasoning</span>
			{:else}
				<span class="preview">{preview}</span>
			{/if}
		</button>
		{#if expanded}
			<div class="narration-body markdown">{@html html}</div>
		{/if}
	</div>
{/if}

<style>
	.body {
		font-size: var(--text-base);
		line-height: 1.5;
		color: var(--text-primary);
		word-break: break-word;
		max-width: 900px;
	}

	/* Settled narration: a dim, de-emphasised disclosure. Collapsed by default. */
	.narration { margin: 0.2rem 0; max-width: 900px; }
	.head {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		width: 100%;
		background: none;
		border: none;
		color: var(--text-dim);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.15rem 0;
		cursor: pointer;
		text-align: left;
		min-width: 0;
	}
	.head:hover { color: var(--text-muted); }
	.chev { display: inline-flex; align-items: center; flex: 0 0 auto; opacity: 0.7; }
	.label { font-style: italic; }
	.preview {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-style: italic;
	}
	.narration-body {
		font-size: var(--text-sm);
		line-height: 1.5;
		color: var(--text-secondary);
		padding: 0.1rem 0 0.2rem 0.9rem;
		border-left: 2px solid var(--border-subtle);
		margin-left: 0.2rem;
		word-break: break-word;
	}
</style>
