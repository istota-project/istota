<script lang="ts">
	import { ChevronRight, ChevronDown, Check, X } from 'lucide-svelte';
	import { isRenderable, type Segment } from '$lib/stores/segments';
	import ToolChip from './ToolChip.svelte';

	// The model's "work" for one assistant turn: inter-tool progress messages
	// (settled narration) and tool calls, in order. Rendered as ONE chip —
	// collapsed shows the current/latest step (progress message + active action
	// together); expanded shows the whole interleaved chain. The final answer is
	// NOT here — it streams prominent outside this component.
	let { steps, streaming = false }: { steps: Segment[]; streaming?: boolean } = $props();

	let expanded = $state(false);

	const renderable = $derived(steps.filter(isRenderable));
	const tools = $derived(
		steps.filter((s): s is Extract<Segment, { kind: 'tool' }> => s.kind === 'tool'),
	);
	const toolCount = $derived(tools.length);

	// The latest progress message (most recent settled narration).
	const latestMessage = $derived.by(() => {
		for (let i = steps.length - 1; i >= 0; i--) {
			const s = steps[i];
			if (s.kind === 'text' && s.text.trim()) return s.text.trim();
		}
		return '';
	});
	// The active action: the running tool, else the most recent one.
	const activeTool = $derived(
		[...tools].reverse().find((t) => t.tool.running)?.tool ?? tools[tools.length - 1]?.tool ?? null,
	);
	const anyRunning = $derived(tools.some((t) => t.tool.running));
	const busy = $derived(streaming || anyRunning);

	// One-line preview for a progress message (first non-empty line).
	function firstLine(text: string): string {
		return text.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '';
	}
</script>

<div class="activity" class:open={expanded} class:active={busy}>
	<button class="head" onclick={() => (expanded = !expanded)} type="button" aria-expanded={expanded}>
		<span class="chev">
			{#if expanded}<ChevronDown size={13} />{:else}<ChevronRight size={13} />{/if}
		</span>
		<!-- Collapsed: current progress message AND current action, together. -->
		<span class="current">
			{#if latestMessage}<span class="msg">{firstLine(latestMessage)}</span>{/if}
			{#if activeTool}
				{#if latestMessage}<span class="sep">·</span>{/if}
				<span class="action">
					<span class="desc">{activeTool.description || activeTool.name}</span>
					{#if !activeTool.running}
						<span class="status">
							{#if activeTool.success === false}<X size={12} />{:else}<Check size={12} />{/if}
						</span>
					{/if}
				</span>
			{/if}
			{#if !latestMessage && !activeTool}<span class="msg">Working…</span>{/if}
		</span>
		{#if toolCount > 0}<span class="count">{toolCount}</span>{/if}
	</button>

	{#if expanded}
		<!-- Expanded: the whole interleaved chain, in order. -->
		<div class="chain">
			{#each renderable as step (step.id)}
				{#if step.kind === 'tool'}
					<ToolChip tool={step.tool} />
				{:else}
					<div class="step-msg">{firstLine(step.text)}</div>
				{/if}
			{/each}
		</div>
	{/if}
</div>

<style>
	.activity {
		margin: 0.3rem 0;
		border-radius: 0.4rem;
		background: var(--surface-badge);
		max-width: 100%;
		width: fit-content;
		min-width: 0;
	}
	.activity.open { width: 100%; }

	/* Active sweep while the turn is live (no spinning icon). */
	.activity.active {
		background: linear-gradient(
			100deg,
			var(--surface-badge) 20%,
			rgba(255, 255, 255, 0.11) 50%,
			var(--surface-badge) 80%
		);
		background-size: 200% 100%;
		animation: activity-pulse 1.5s ease-in-out infinite;
	}
	@keyframes activity-pulse {
		from { background-position: 150% 0; }
		to { background-position: -150% 0; }
	}
	@media (prefers-reduced-motion: reduce) {
		.activity.active { animation: none; background: var(--surface-badge); }
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

	.chev { display: inline-flex; align-items: center; flex: 0 0 auto; opacity: 0.6; }

	.current {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 0.35rem;
		overflow: hidden;
		white-space: nowrap;
	}
	.msg {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-style: italic;
		color: var(--text-dim);
	}
	.sep { flex: 0 0 auto; opacity: 0.5; }
	.action {
		flex: 0 1 auto;
		min-width: 0;
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		overflow: hidden;
	}
	.desc {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: ui-monospace, monospace;
	}
	.status { display: inline-flex; align-items: center; flex: 0 0 auto; }
	.count {
		flex: 0 0 auto;
		font-variant-numeric: tabular-nums;
		background: var(--surface-base);
		border-radius: var(--radius-pill);
		padding: 0 0.4rem;
		font-size: 0.65rem;
		opacity: 0.8;
	}

	.chain {
		border-top: 1px solid var(--border-subtle);
		padding: 0.35rem 0.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.step-msg {
		font-style: italic;
		font-size: var(--text-xs);
		color: var(--text-dim);
		word-break: break-word;
		white-space: pre-wrap;
	}

	/* Light theme — the active shimmer washes out on a light surface; use a
	   subtle dark tint instead. */
	:global(:root[data-theme='light']) .activity.active {
		background: linear-gradient(
			100deg,
			var(--surface-badge) 20%,
			rgba(0, 0, 0, 0.06) 50%,
			var(--surface-badge) 80%
		);
		background-size: 200% 100%;
	}
</style>
