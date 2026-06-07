<script lang="ts">
	import { renderMarkdown } from '$lib/markdown';
	import type { ChatMessage } from '$lib/stores/chat';
	import ToolChip from './ToolChip.svelte';
	import ConfirmationCard from './ConfirmationCard.svelte';

	let {
		message,
		onConfirm,
		onReject,
	}: {
		message: ChatMessage;
		onConfirm: (cid: number, taskId: number) => void;
		onReject: (cid: number, taskId: number) => void;
	} = $props();

	const isUser = $derived(message.role === 'user');
	const isSystem = $derived(message.role === 'system');
	// User text is shown verbatim (escaped) — we don't render their input as
	// markdown. Bot/system text is rendered via the safe markdown renderer.
	const bodyHtml = $derived(isUser ? '' : renderMarkdown(message.text));
</script>

<div class="msg-row" class:user={isUser} class:system={isSystem}>
	<div class="bubble" class:user={isUser} class:system={isSystem} class:error={message.error}>
		{#if message.tools.length}
			<div class="tools">
				{#each message.tools as tool (tool.id)}
					<ToolChip {tool} />
				{/each}
			</div>
		{/if}

		{#if message.streaming && !message.text}
			<div class="progress">
				<span class="dot"></span>
				<span>{message.progress || 'Thinking…'}</span>
			</div>
		{:else if isUser}
			<div class="body user-body">{message.text}</div>
		{:else}
			<div class="body markdown">{@html bodyHtml}</div>
			{#if message.streaming && message.progress}
				<div class="progress subtle"><span class="dot"></span><span>{message.progress}</span></div>
			{/if}
		{/if}

		{#if message.confirmation && message.taskId}
			<ConfirmationCard
				onConfirm={() => onConfirm(message.cid, message.taskId!)}
				onReject={() => onReject(message.cid, message.taskId!)}
			/>
		{/if}
	</div>
</div>

<style>
	.msg-row {
		display: flex;
		margin: 0.4rem 0;
	}
	.msg-row.user { justify-content: flex-end; }
	.msg-row.system { justify-content: center; }

	.bubble {
		max-width: 80%;
		padding: 0.5rem 0.75rem;
		border-radius: var(--radius-card);
		font-size: var(--text-base);
		line-height: 1.5;
		color: var(--text-primary);
		background: var(--surface-card);
		border: 1px solid var(--border-subtle);
		word-break: break-word;
	}
	.bubble.user {
		background: var(--surface-raised);
		border-color: var(--border-default);
	}
	.bubble.system {
		max-width: 90%;
		background: var(--surface-base);
		color: var(--text-muted);
		font-size: var(--text-sm);
	}
	.bubble.error {
		border-color: #6b3a3a;
		color: #e0a0a0;
	}

	.user-body { white-space: pre-wrap; }

	.tools {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
		margin-bottom: 0.4rem;
	}

	.progress {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		color: var(--text-muted);
		font-size: var(--text-sm);
	}
	.progress.subtle { margin-top: 0.3rem; color: var(--text-dim); }
	.dot {
		width: 6px; height: 6px; border-radius: 50%;
		background: var(--text-muted);
		animation: pulse 1.1s ease-in-out infinite;
	}
	@keyframes pulse { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }

	/* Markdown content spacing — kept tight for chat. */
	.markdown :global(p) { margin: 0 0 0.5rem; }
	.markdown :global(p:last-child) { margin-bottom: 0; }
	.markdown :global(ul),
	.markdown :global(ol) { margin: 0 0 0.5rem; padding-left: 1.2rem; }
	.markdown :global(h1),
	.markdown :global(h2),
	.markdown :global(h3),
	.markdown :global(h4) { margin: 0.3rem 0; font-size: var(--text-base); font-weight: 600; }
	.markdown :global(code) {
		font-family: ui-monospace, monospace;
		font-size: 0.9em;
		background: var(--surface-base);
		padding: 0.05rem 0.3rem;
		border-radius: 0.25rem;
	}
	.markdown :global(pre) {
		background: var(--surface-base);
		border: 1px solid var(--border-subtle);
		border-radius: 0.35rem;
		padding: 0.5rem 0.65rem;
		overflow-x: auto;
		margin: 0 0 0.5rem;
	}
	.markdown :global(pre code) { background: none; padding: 0; }
	.markdown :global(a) { color: var(--map-path); }
</style>
