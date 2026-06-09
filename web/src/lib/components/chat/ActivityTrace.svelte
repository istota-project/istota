<script lang="ts">
	import { ChevronRight, ChevronDown, X } from 'lucide-svelte';
	import type { Segment } from '$lib/stores/segments';

	// The model's tool calls for one assistant turn, in order. Rendered as ONE
	// chip — collapsed shows the active action; expanded lists every call. The
	// model's reasoning and narration are NOT shown (the actions are descriptive
	// enough); the final answer streams prominent outside this component.
	let { steps, streaming = false }: { steps: Segment[]; streaming?: boolean } = $props();

	let expanded = $state(false);

	const tools = $derived(
		steps.filter((s): s is Extract<Segment, { kind: 'tool' }> => s.kind === 'tool'),
	);
	const toolCount = $derived(tools.length);
	// The active action: the running tool, else the most recent one.
	const activeTool = $derived(
		[...tools].reverse().find((t) => t.tool.running)?.tool ?? tools[tools.length - 1]?.tool ?? null,
	);
	const anyRunning = $derived(tools.some((t) => t.tool.running));
	const busy = $derived(streaming || anyRunning);
</script>

<div class="activity" class:open={expanded} class:active={busy}>
	<button class="head" onclick={() => (expanded = !expanded)} type="button" aria-expanded={expanded}>
		<span class="chev">
			{#if expanded}<ChevronDown size={13} />{:else}<ChevronRight size={13} />{/if}
		</span>
		<!-- Collapsed: the active action. Expanded: a static label only — every
		     call is listed in the chain below, so repeating the latest here would
		     duplicate the last row. -->
		<span class="current">
			{#if expanded}
				<span class="msg label">{busy ? 'Working…' : 'Activity'}</span>
			{:else if activeTool}
				<span class="action">
					{#if activeTool.success === false}
						<span class="status"><X size={12} /></span>
					{/if}
					<span class="desc">{activeTool.description || activeTool.name}</span>
				</span>
			{:else}
				<span class="msg">Working…</span>
			{/if}
		</span>
		{#if toolCount > 0}<span class="count">{toolCount}</span>{/if}
	</button>

	{#if expanded}
		<!-- Expanded: every tool call, flat rows in order. -->
		<div class="chain">
			{#each tools as step (step.id)}
				<div class="action chain-action">
					{#if step.tool.success === false}
						<span class="status"><X size={12} /></span>
					{/if}
					<span class="desc">{step.tool.description || step.tool.name}</span>
				</div>
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

	/* Current step: the active action (or a quiet label when expanded). */
	.current {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		overflow: hidden;
	}
	.msg {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-style: italic;
		color: var(--text-dim);
	}
	/* Expanded-header label: a quiet stand-in for the live action (which moves
	   into the chain below when expanded). */
	.msg.label { font-style: normal; opacity: 0.7; }
	.action {
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 0.3rem;
		overflow: hidden;
	}
	.chain-action { padding: 0.05rem 0; }
	.desc {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: ui-monospace, monospace;
		color: var(--text-secondary);
	}
	/* Failed tools keep an X; success and in-progress tools show description only
	   (no checkmark, no running dot — the chip's active shimmer signals work). */
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
		/* Left padding aligns the chain with the header's content (past the
		   caret): head padding (0.5rem) + chevron (13px) + head gap (0.4rem). */
		padding: 0.35rem 0.5rem 0.4rem calc(0.5rem + 13px + 0.4rem);
		font-size: var(--text-xs);
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}

	/* Light theme — lighter-gray chip fill (cascades into the .active gradient
	   below, which reads the same --surface-badge). */
	:global(:root[data-theme='light']) .activity {
		--surface-badge: #eeeef0;
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
