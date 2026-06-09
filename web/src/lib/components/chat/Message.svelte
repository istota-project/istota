<script lang="ts">
	import { renderMarkdown } from '$lib/markdown';
	import type { ChatMessage } from '$lib/stores/chat';
	import type { Segment } from '$lib/stores/segments';
	import ActivityTrace from './ActivityTrace.svelte';
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

	// System (!command) output goes through the safe markdown renderer; user text
	// is shown verbatim and the assistant body is rendered below.
	const bodyHtml = $derived(isSystem ? renderMarkdown(message.text) : '');

	// Split the turn into "work" (inter-tool narration + tool calls → the single
	// ActivityTrace chip) and the "answer" (the final response → prominent,
	// streamed markdown). The answer is always the trailing text segment: settling
	// only happens alongside a tool push, so a text segment is the last segment
	// iff it's the open/answer block. Everything before it is work.
	const segments = $derived(message.segments);
	const answerSeg = $derived.by<Extract<Segment, { kind: 'text' }> | null>(() => {
		const last = segments[segments.length - 1];
		return last && last.kind === 'text' ? last : null;
	});
	const workSegments = $derived(answerSeg ? segments.slice(0, -1) : segments);
	const toolCount = $derived(message.segments.filter((s) => s.kind === 'tool').length);
	// The activity chip is tool-only — it appears solely when the turn made tool
	// calls. Reasoning/narration are not shown; before any tool exists, the work
	// phase is represented by the pulsing "Thinking…" cue instead.
	const hasTools = $derived(toolCount > 0);
	const hasAnswerText = $derived(!!(answerSeg && answerSeg.text));

	// Subtle per-message metadata, revealed on hover (bottom-right).
	const meta = $derived.by(() => {
		const parts: string[] = [];
		if (message.taskId) parts.push(`#${message.taskId}`);
		// Drop a provider prefix (e.g. `anthropic/`) then a leading `claude-` for
		// a compact label; native/openrouter slugs keep their distinguishing tail.
		if (message.model) parts.push(message.model.replace(/^[^/]+\//, '').replace(/^claude-/, ''));
		if (typeof message.durationSeconds === 'number') parts.push(`${message.durationSeconds}s`);
		if (toolCount) parts.push(`${toolCount} tool${toolCount === 1 ? '' : 's'}`);
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

			{#if isUser}
				{#if message.text}<div class="body user-body">{message.text}</div>{/if}
				{#if message.attachments?.length}
					<div class="attachments">
						{#each message.attachments as name}
							<span class="attachment">📎 {name}</span>
						{/each}
					</div>
				{/if}
			{:else}
				<!-- The model's tool calls fold into one activity chip; the final
				     answer streams prominent below it. Reasoning/narration are not
				     shown — the work phase before any tool is the cue below. -->
				{#if hasTools}
					<ActivityTrace steps={workSegments} streaming={message.streaming} />
				{/if}

				{#if hasAnswerText}
					<div class="body markdown">{@html renderMarkdown(answerSeg!.text)}</div>
				{/if}

				{#if message.streaming && !hasTools && !hasAnswerText}
					<!-- Work-phase cue: the ack verb + pulsing dot, shown while the
					     model reasons / before the first tool or answer text. -->
					<div class="progress">
						<span class="dot"></span>
						<span class="status-text">{message.progress || 'Thinking…'}</span>
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

	/* Markdown block styling is global (src/app.css `.markdown`) so it applies
	   across component boundaries — the answer body and command output both
	   render through the same `.markdown` container class. */

	/* Light theme — dark-tuned chat colors remapped for white surfaces.
	   Dark rules above are untouched. */
	:global(:root[data-theme='light']) .msg.error .body,
	:global(:root[data-theme='light']) .cmd-output.error {
		color: #c0271d;
	}
	/* The amber accent darkens in light mode, so the bot initial needs light
	   text for contrast (dark mode keeps its dark initial on bright amber). */
	:global(:root[data-theme='light']) .avatar.bot { color: #fff; }
	/* The chat pane is --surface-card (#f4f4f5) in light mode, so the default
	   --surface-raised hover (#e8e8ea) would darken the row. Hover toward white
	   instead so the highlight reads as *lighter* than the pane, matching dark
	   mode's direction. */
	:global(:root[data-theme='light']) .msg:hover { background: var(--surface-base); }
</style>
