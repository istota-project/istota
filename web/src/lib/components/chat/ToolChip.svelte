<script lang="ts">
	import { Check, X, ChevronRight, ChevronDown } from 'lucide-svelte';
	import type { ToolEntry } from '$lib/stores/chat';

	// One inline, expandable tool chip rendered at its true position in the
	// assistant turn. Pulses while its own tool runs; shows a Check/X once done;
	// the live output tail (`progress`) is shown only in the expanded view and
	// only while running (cleared on tool_end).
	let { tool }: { tool: ToolEntry } = $props();

	let expanded = $state(false);

	// Collapsed label prefers the model's own action description over the output
	// tail, so a finished chip reads "⚙️ Generate round 1…", not "}".
	const label = $derived(tool.description || `Using ${tool.name}`);
</script>

<div class="tool-chip" class:open={expanded} class:active={tool.running}>
	<button class="head" onclick={() => (expanded = !expanded)} type="button" aria-expanded={expanded}>
		{#if !tool.running}
			<span class="status">
				{#if tool.success === false}<X size={13} />{:else}<Check size={13} />{/if}
			</span>
		{/if}
		<span class="summary">{label}</span>
		<span class="chev">
			{#if expanded}<ChevronDown size={13} />{:else}<ChevronRight size={13} />{/if}
		</span>
	</button>

	{#if expanded}
		<div class="detail">
			<span class="desc">{tool.description || tool.name}</span>
			{#if tool.running && tool.progress}<span class="progress">{tool.progress}</span>{/if}
		</div>
	{/if}
</div>

<style>
	.tool-chip {
		margin: 0.25rem 0;
		border-radius: 0.4rem;
		background: var(--surface-badge);
		max-width: 100%;
		width: fit-content;
		min-width: 0;
	}
	.tool-chip.open { width: 100%; }

	/* Active state: a subtle gradient sweeps across the chip while its tool runs
	   (no spinning icon). */
	.tool-chip.active {
		background: linear-gradient(
			100deg,
			var(--surface-badge) 20%,
			rgba(255, 255, 255, 0.11) 50%,
			var(--surface-badge) 80%
		);
		background-size: 200% 100%;
		animation: tool-pulse 1.5s ease-in-out infinite;
	}
	@keyframes tool-pulse {
		from { background-position: 150% 0; }
		to { background-position: -150% 0; }
	}
	@media (prefers-reduced-motion: reduce) {
		.tool-chip.active { animation: none; background: var(--surface-badge); }
	}

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

	.detail {
		border-top: 1px solid var(--border-subtle);
		padding: 0.3rem 0.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
	}
	.desc {
		font-family: ui-monospace, monospace;
		font-size: var(--text-xs);
		color: var(--text-secondary);
		word-break: break-word;
	}
	.progress {
		font-family: ui-monospace, monospace;
		font-size: var(--text-xs);
		color: var(--text-dim);
		white-space: pre-wrap;
		word-break: break-word;
	}

	/* Light theme — the active-state shimmer sweeps a white highlight that washes
	   out on a light surface, so use a subtle dark tint instead. */
	:global(:root[data-theme='light']) .tool-chip.active {
		background: linear-gradient(
			100deg,
			var(--surface-badge) 20%,
			rgba(0, 0, 0, 0.06) 50%,
			var(--surface-badge) 80%
		);
		background-size: 200% 100%;
	}
</style>
