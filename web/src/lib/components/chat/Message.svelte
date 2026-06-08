<script lang="ts">
	import { renderMarkdown } from '$lib/markdown';
	import type { ChatMessage } from '$lib/stores/chat';
	import ToolStrip from './ToolStrip.svelte';
	import ConfirmationCard from './ConfirmationCard.svelte';

	let {
		message,
		continuation = false,
		userName = 'You',
		botName = 'Istota',
		onConfirm,
		onReject,
	}: {
		message: ChatMessage;
		// True when this message continues a run from the same author, so the
		// avatar + author/time header is collapsed (Discord/Slack grouping).
		continuation?: boolean;
		userName?: string;
		botName?: string;
		onConfirm: (cid: number, taskId: number) => void;
		onReject: (cid: number, taskId: number) => void;
	} = $props();

	const isUser = $derived(message.role === 'user');
	const isSystem = $derived(message.role === 'system');
	const author = $derived(isUser ? userName : botName);
	const initial = $derived((author.trim()[0] ?? '?').toUpperCase());

	// User text is shown verbatim (escaped via text binding) — we don't render
	// their input as markdown. Bot text goes through the safe markdown renderer.
	const bodyHtml = $derived(isUser ? '' : renderMarkdown(message.text));

	const hasRunningTool = $derived(message.tools.some((t) => t.running));

	// Subtle per-message metadata, revealed on hover (bottom-right).
	const meta = $derived.by(() => {
		const parts: string[] = [];
		if (message.taskId) parts.push(`#${message.taskId}`);
		if (typeof message.durationSeconds === 'number') parts.push(`${message.durationSeconds}s`);
		if (message.tools.length) {
			parts.push(`${message.tools.length} tool${message.tools.length === 1 ? '' : 's'}`);
		}
		return parts;
	});

	const time = $derived.by(() => {
		if (!message.createdAt) return '';
		const d = new Date(message.createdAt);
		if (Number.isNaN(d.getTime())) return '';
		return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	});
</script>

{#if isSystem}
	<!-- Command (!…) output. Left-aligned block, not a centered notice: it
	     carries lists / code / tables that must read left-to-right. -->
	<div class="cmd-row">
		<div class="cmd-output markdown" class:error={message.error}>{@html bodyHtml}</div>
	</div>
{:else}
	<div class="msg" class:continuation class:error={message.error}>
		<div class="gutter">
			{#if continuation}
				<time class="hover-time">{time}</time>
			{:else}
				<div class="avatar" class:bot={!isUser}>{initial}</div>
			{/if}
		</div>

		<div class="content">
			{#if !continuation}
				<div class="meta">
					<span class="author" class:bot={!isUser}>{author}</span>
					{#if time}<time class="stamp">{time}</time>{/if}
				</div>
			{/if}

			{#if message.tools.length}
				<ToolStrip tools={message.tools} streaming={message.streaming} />
			{/if}

			{#if message.streaming && !message.text}
				{#if !hasRunningTool && !message.tools.length}
					<!-- Pre-tool / pure-thinking cue. Suppressed once any tool
					     appears — the ToolStrip is then the single activity
					     indicator (it keeps pulsing between tools while streaming),
					     so the dot doesn't flicker in and out per tool call. -->
					<div class="progress">
						<span class="dot"></span>
						<span class="status-text">{message.progress || 'Thinking…'}</span>
					</div>
				{/if}
			{:else if isUser}
				{#if message.text}<div class="body user-body">{message.text}</div>{/if}
				{#if message.attachments?.length}
					<div class="attachments">
						{#each message.attachments as name}
							<span class="attachment">📎 {name}</span>
						{/each}
					</div>
				{/if}
			{:else}
				<div class="body markdown">{@html bodyHtml}</div>
				{#if message.streaming && !hasRunningTool && !message.tools.length}
					<!-- Typing affordance while the answer streams, for the
					     no-tools case only. When tools are present the ToolStrip
					     carries the "still working" signal, so we don't also show
					     this dot (it would flicker in after each tool completes). -->
					<div class="progress subtle">
						<span class="dot"></span>
						{#if message.progress}<span class="status-text">{message.progress}</span>{/if}
					</div>
				{/if}
			{/if}

			{#if message.confirmation && message.taskId}
				<ConfirmationCard
					onConfirm={() => onConfirm(message.cid, message.taskId!)}
					onReject={() => onReject(message.cid, message.taskId!)}
				/>
			{/if}
		</div>

		{#if meta.length && !message.streaming}
			<div class="meta-footer">{meta.join(' · ')}</div>
		{/if}
	</div>
{/if}

<style>
	/* Discord/Slack-style row: avatar gutter on the left, author + time header,
	   then the message body. Consecutive messages from the same author collapse
	   into one visual group (the `.continuation` rows hide the header). */
	.msg {
		display: flex;
		gap: 0.6rem;
		/* Extra bottom padding so the hover highlight isn't flush with the last
		   line of text. */
		padding: 0.1rem 0.75rem 0.45rem;
		align-items: flex-start;
		/* Anchor for the absolutely-positioned .meta-footer (top-right). */
		position: relative;
	}
	.msg:not(.continuation) { margin-top: 0.7rem; padding-top: 0.45rem; }
	.msg:hover { background: var(--surface-raised); }
	.msg:hover .hover-time { opacity: 1; }
	.msg:hover .meta-footer { opacity: 1; }

	/* Subtle per-message metadata at the top-right, revealed on hover. Absolutely
	   positioned so it overlays the row's top-right corner instead of consuming a
	   flex column — otherwise it narrows the message content (badly on mobile). */
	.meta-footer {
		position: absolute;
		top: 0.3rem;
		right: 0.75rem;
		pointer-events: none;
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
		opacity: 0;
		transition: opacity var(--transition-fast);
	}

	.gutter {
		flex: 0 0 2.25rem;
		display: flex;
		justify-content: center;
		padding-top: 0.1rem;
	}
	.avatar {
		width: 2.1rem;
		height: 2.1rem;
		border-radius: 0.5rem;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 0.85rem;
		font-weight: 600;
		color: #fff;
		background: #4a4a52;
		user-select: none;
	}
	.avatar.bot { background: var(--accent-amber); color: #111; }

	.hover-time {
		font-size: 0.62rem;
		color: var(--text-dim);
		opacity: 0;
		line-height: 1.6;
		transition: opacity var(--transition-fast);
		font-variant-numeric: tabular-nums;
	}

	.content { flex: 1; min-width: 0; }

	.meta {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		margin-bottom: 0.1rem;
	}
	.author {
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
	}
	.author.bot { color: var(--accent-amber); }
	.stamp {
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
	}

	.body {
		font-size: var(--text-base);
		line-height: 1.5;
		color: var(--text-primary);
		word-break: break-word;
		/* Cap readable content width so long lines / wide blocks stay legible;
		   the row itself stays full-width so the hover highlight spans it. */
		max-width: 900px;
	}
	.user-body { white-space: pre-wrap; }

	.msg.error .body,
	.cmd-output.error { color: #e0a0a0; }

	/* Command (!…) output: a left-aligned block set apart from the conversation
	   by a subtle card, so its lists / code / tables render left-to-right. */
	.cmd-row {
		padding: 0.2rem 0.75rem 0.5rem;
	}
	.cmd-output {
		max-width: 900px;
		font-size: var(--text-sm);
		line-height: 1.5;
		color: var(--text-secondary);
		background: var(--surface-raised);
		border: 1px solid var(--border-subtle);
		border-radius: 0.4rem;
		padding: 0.5rem 0.75rem;
		text-align: left;
		word-break: break-word;
	}

	.attachments { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.3rem; }
	.attachment {
		font-size: var(--text-xs);
		color: var(--text-muted);
		background: var(--surface-base);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-pill);
		padding: 0.1rem 0.45rem;
	}

	.progress {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		min-width: 0;
		color: var(--text-muted);
		font-size: var(--text-sm);
	}
	.progress.subtle { margin-top: 0.3rem; color: var(--text-dim); }
	/* Tool descriptions (e.g. a long shell command) shouldn't wrap the row. */
	.status-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dot { flex: 0 0 auto; }
	.dot {
		width: 6px; height: 6px; border-radius: 50%;
		background: var(--text-muted);
		animation: pulse 1.1s ease-in-out infinite;
	}
	@keyframes pulse { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }

	/* Markdown content spacing — roomy enough to separate blocks clearly. */
	.markdown :global(p) { margin: 0 0 1rem; }
	.markdown :global(p:last-child) { margin-bottom: 0; }
	.markdown :global(ul),
	.markdown :global(ol) { margin: 0 0 1rem; padding-left: 1.3rem; }
	.markdown :global(li) { margin: 0.1rem 0; }
	.markdown :global(li > ul),
	.markdown :global(li > ol) { margin: 0.1rem 0; }
	.markdown :global(h1),
	.markdown :global(h2),
	.markdown :global(h3),
	.markdown :global(h4) { margin: 0.4rem 0 0.3rem; font-size: var(--text-base); font-weight: 600; }
	.markdown :global(blockquote) {
		margin: 0 0 1rem;
		padding: 0.1rem 0.7rem;
		border-left: 3px solid var(--border-default);
		color: var(--text-secondary);
	}
	.markdown :global(code) {
		font-family: ui-monospace, monospace;
		font-size: 0.9em;
		background: #2d2d2d;
		color: #e6e6e6;
		padding: 0.05rem 0.3rem;
		border-radius: 0.25rem;
	}
	.markdown :global(pre) {
		background: #2d2d2d;
		color: #e6e6e6;
		border: 1px solid var(--border-subtle);
		border-radius: 0.35rem;
		padding: 0.5rem 0.65rem;
		overflow-x: auto;
		margin: 0 0 1rem;
	}
	.markdown :global(pre code) { background: none; padding: 0; }
	.markdown :global(a) { color: var(--accent-amber); }
	.markdown :global(del) { color: var(--text-muted); }
	.markdown :global(table) {
		border-collapse: collapse;
		margin: 0 0 1rem;
		font-size: var(--text-sm);
		display: block;
		overflow-x: auto;
	}
	.markdown :global(th),
	.markdown :global(td) {
		border: 1px solid var(--border-subtle);
		padding: 0.2rem 0.45rem;
		text-align: left;
	}
	.markdown :global(th) { background: var(--surface-base); font-weight: 600; }

	/* Light theme — dark-tuned chat colors remapped for white surfaces.
	   Dark rules above are untouched. */
	:global(:root[data-theme='light']) .msg.error .body,
	:global(:root[data-theme='light']) .cmd-output.error {
		color: #c0271d;
	}
	:global(:root[data-theme='light']) .markdown :global(code),
	:global(:root[data-theme='light']) .markdown :global(pre) {
		background: #ececef;
		color: #1a1a1a;
	}
</style>
