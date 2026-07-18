<script lang="ts">
	import { CornerDownRight, MessageSquare, FileText } from 'lucide-svelte';
	import type { SearchResultsData, SearchResultItem } from '$lib/stores/segments';

	let {
		data,
		onJump,
	}: {
		data: SearchResultsData;
		// Jump to a conversation result's assistant turn. Given the result's room
		// token + task id; the store selects the room (if needed) and scrolls.
		// Absent → cards render without the jump affordance (graceful degradation).
		onJump?: (roomToken: string, taskId: number) => void;
	} = $props();

	const MEMORY_TYPES = new Set([
		'memory_file',
		'user_memory',
		'channel_memory',
		'channel_memory_durable',
	]);

	function isMemory(r: SearchResultItem): boolean {
		return MEMORY_TYPES.has(r.source_type);
	}

	// A conversation result can be jumped to only when we have both a task id and
	// a room token to resolve it in.
	function canJump(r: SearchResultItem): boolean {
		return (
			!!onJump &&
			r.source_type === 'conversation' &&
			typeof r.task_id === 'number' &&
			!!r.room_token
		);
	}

	// A friendly label for a memory result's source.
	function memoryLabel(r: SearchResultItem): string {
		if (r.source_type === 'user_memory') return 'USER.md';
		if (r.source_type === 'channel_memory' || r.source_type === 'channel_memory_durable')
			return r.room_name || 'CHANNEL.md';
		return 'Memory';
	}
</script>

<div class="search-results">
	{#if data.results.length === 0}
		<p class="empty">No results for “{data.query}”.</p>
	{:else}
		<p class="head">
			{data.results.length} result{data.results.length === 1 ? '' : 's'} for “{data.query}”
		</p>
		<ul class="cards">
			{#each data.results as r, i (i)}
				<li class="card" class:memory={isMemory(r)}>
					<div class="card-head">
						<span class="icon" aria-hidden="true">
							{#if isMemory(r)}
								<FileText size={14} />
							{:else}
								<MessageSquare size={14} />
							{/if}
						</span>
						<span class="source">
							{#if isMemory(r)}
								{memoryLabel(r)}
							{:else}
								{r.room_name || 'Conversation'}
							{/if}
						</span>
						{#if r.date}<span class="date">{r.date}</span>{/if}
					</div>
					<div class="summary">{r.summary}</div>
					<div class="card-actions">
						{#if canJump(r)}
							<button
								class="jump-btn"
								type="button"
								onclick={() => onJump?.(r.room_token!, r.task_id!)}
							>
								<CornerDownRight size={12} />
								Jump to reply
							</button>
						{:else if r.talk_link}
							<a class="talk-link" href={r.talk_link} target="_blank" rel="noopener noreferrer">
								Open in Talk
							</a>
						{/if}
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.search-results {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		max-width: 46rem;
	}
	.head,
	.empty {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--text-muted);
	}
	.cards {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.card {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-card);
		background: var(--surface-raised);
		padding: 0.5rem 0.65rem;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.card.memory {
		background: var(--surface-base);
	}
	.card-head {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--text-xs);
		color: var(--text-muted);
	}
	.icon {
		display: inline-flex;
		color: var(--text-dim);
	}
	.source {
		font-weight: 600;
		color: var(--text-secondary);
	}
	.date {
		margin-left: auto;
		color: var(--text-dim);
	}
	.summary {
		font-size: var(--text-sm);
		color: var(--text-primary);
		line-height: 1.4;
	}
	.card-actions {
		display: flex;
	}
	.jump-btn {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		background: none;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-pill);
		color: var(--text-muted);
		font-size: var(--text-xs);
		padding: 0.15rem 0.55rem;
		cursor: pointer;
	}
	.jump-btn:hover {
		color: var(--text-primary);
		border-color: var(--text-dim);
	}
	.talk-link {
		font-size: var(--text-xs);
		color: var(--accent-amber);
	}
</style>
