<script lang="ts">
  import { Star } from 'lucide-svelte';
  import { renderMarkdown } from '$lib/markdown';
  import type { ChatMessage } from '$lib/stores/chat';
  import { renderGroups } from '$lib/stores/segments';
  import ActivityTrace from './ActivityTrace.svelte';
  import ConfirmationCard from './ConfirmationCard.svelte';
  import SearchResults from './SearchResults.svelte';

  let {
    message,
    continuation = false,
    userName = 'You',
    botName = 'Istota',
    onConfirm,
    onReject,
    onToggleStar,
    onRoomClick,
    onJump,
    aggregate = false,
  }: {
    message: ChatMessage;
    // True when this message continues a run from the same author, so the
    // avatar + author/time header is collapsed (Discord/Slack grouping).
    continuation?: boolean;
    userName?: string;
    botName?: string;
    onConfirm: (cid: number, taskId: number) => void;
    onReject: (cid: number, taskId: number) => void;
    // Star toggle for durable messages (rows carrying msgId). Absent → no
    // star affordance (e.g. surfaces that don't support starring).
    onToggleStar?: (cid: number) => void;
    // Aggregate views: click the message's room label to jump into that room.
    // Only rendered when both the handler and message.roomName are present.
    onRoomClick?: (token: string) => void;
    // Jump to a search result's conversation turn (room token + task id).
    // Passed to a search_results system row's cards; absent elsewhere.
    onJump?: (roomToken: string, taskId: number) => void;
    // True in the cross-room views (All messages / Unread / Starred), where
    // the hover bar carries only the task number — model and timings are
    // room-level detail that belongs in the room view.
    aggregate?: boolean;
  } = $props();

  const isUser = $derived(message.role === 'user');
  const isSystem = $derived(message.role === 'system');
  const author = $derived(isUser ? userName : botName);
  const initial = $derived((author.trim()[0] ?? '?').toUpperCase());

  // System (!command) output goes through the safe markdown renderer; user text
  // is shown verbatim and the assistant body is rendered below.
  const bodyHtml = $derived(isSystem ? renderMarkdown(message.text) : '');

  // The turn's body is an ordered list of render groups (substantial prose +
  // activity chips), interleaved in the model's true block order. A substantial
  // intermediate text block — analysis the model wrote, then acted on — renders
  // as its own prominent prose group rather than vanishing into a tool-only
  // chip; short lead-in narration is dropped. The trailing text is always the
  // answer. See renderGroups for the rule.
  const groups = $derived(renderGroups(message));
  const toolCount = $derived(message.segments.filter((s) => s.kind === 'tool').length);
  // Index of the last activity group, so only the trailing chip pulses while
  // the message is still streaming.
  const lastActivityIdx = $derived.by(() => {
    for (let i = groups.length - 1; i >= 0; i--) if (groups[i].kind === 'activity') return i;
    return -1;
  });

  // Subtle per-message metadata, revealed on hover (bottom-right).
  const meta = $derived.by(() => {
    const parts: string[] = [];
    if (message.taskId) parts.push(`#${message.taskId}`);
    if (aggregate) return parts;
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

  // Star affordance: durable messages only (msgId = the messages-store row),
  // and only when the surface passes a toggle handler.
  const starrable = $derived(typeof message.msgId === 'number' && !!onToggleStar);
  const showRoomChip = $derived(!!message.roomName && !!onRoomClick);
</script>

{#snippet starButton()}
  <button
    class="star-btn"
    class:starred={message.starred}
    onclick={() => onToggleStar?.(message.cid)}
    aria-label={message.starred ? 'Unstar message' : 'Star message'}
    aria-pressed={message.starred ? 'true' : 'false'}
    title={message.starred ? 'Unstar' : 'Star'}
    type="button"
  >
    <Star size={14} fill={message.starred ? 'currentColor' : 'none'} />
  </button>
{/snippet}

{#if isSystem}
  <!-- Command (!…) output / delivered notifications. Left-aligned block, not a
	     centered notice: it carries lists / code / tables that must read
	     left-to-right. Durable system rows (msgId) are starrable too. -->
  <div class="cmd-row" data-cid={message.cid} data-task-id={message.taskId ?? undefined}>
    {#if showRoomChip}
      <button class="room-chip" onclick={() => onRoomClick?.(message.roomToken!)} type="button">
        {message.roomName}
      </button>
    {/if}
    {#if message.searchResults}
      <SearchResults data={message.searchResults} {onJump} />
    {:else}
      <div class="cmd-output markdown" class:error={message.error}>{@html bodyHtml}</div>
    {/if}
    {#if starrable}
      <div class="msg-actions cmd-actions">
        {@render starButton()}
      </div>
    {/if}
  </div>
{:else}
  <div
    class="msg"
    class:continuation
    class:error={message.error}
    data-cid={message.cid}
    data-task-id={message.taskId ?? undefined}
  >
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
          {#if showRoomChip}
            <button
              class="room-chip"
              onclick={() => onRoomClick?.(message.roomToken!)}
              type="button"
              title="Go to room"
            >
              {message.roomName}
            </button>
          {/if}
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
        <!-- The turn renders as ordered groups: substantial prose blocks
				     (prominent markdown) interleaved with activity chips (tool runs
				     fold into one chip each). Short lead-in narration and reasoning
				     are dropped — the pre-tool work phase is the cue below. -->
        {#each groups as g, gi (g.id)}
          {#if g.kind === 'activity'}
            <!-- A chip sandwiched between paragraphs needs room to breathe;
						     the first group sits tight under the meta, like a no-tool
						     text answer. Spacing is neighbour-aware (chips never abut —
						     tool runs coalesce — so a chip's neighbours are prose or the
						     message edge). -->
            <div
              class="chip-slot"
              class:gap-above={groups[gi - 1]?.kind === 'prose'}
              class:gap-below={groups[gi + 1]?.kind === 'prose'}
            >
              <ActivityTrace
                steps={g.steps}
                streaming={message.streaming && gi === lastActivityIdx}
              />
            </div>
          {:else}
            <div class="body markdown">{@html renderMarkdown(g.text)}</div>
          {/if}
        {/each}

        {#if message.streaming && groups.length === 0}
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

    <!-- Floating per-message actions bar (top-right): the hover metadata plus
		     the star toggle. One bar so the two hover surfaces can't collide; a
		     starred message keeps its star visible at rest. Future per-message
		     actions (copy, …) land here. -->
    {#if starrable || (meta.length && !message.streaming)}
      <div class="msg-actions">
        {#if meta.length && !message.streaming}
          <span class="meta-footer">{meta.join(' · ')}</span>
        {/if}
        {#if starrable}
          {@render starButton()}
        {/if}
      </div>
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
  .msg:not(.continuation) {
    margin-top: 0.7rem;
    padding-top: 0.45rem;
  }
  .msg:hover .hover-time {
    opacity: 1;
  }
  .msg:hover .meta-footer {
    opacity: 1;
  }

  /* Floating per-message actions bar at the top-right, holding the hover
	   metadata and the star toggle. Absolutely positioned so it overlays the
	   row's top-right corner instead of consuming a flex column — otherwise it
	   narrows the message content (badly on mobile). `top` is set per row-type
	   below so its baseline lines up with the time on the left, which lives in
	   different spots: the author header on a fresh group, the gutter on a
	   continuation. */
  .msg-actions {
    position: absolute;
    right: 0.75rem;
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Fresh group: time sits in the .meta author header (next to the name). */
  .msg:not(.continuation) .msg-actions {
    top: 0.6rem;
  }
  /* Continuation: time sits in the left gutter (.hover-time), higher up. */
  .msg.continuation .msg-actions {
    top: 0.15rem;
  }

  /* Subtle per-message metadata, revealed on hover (child of the actions bar). */
  .meta-footer {
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    opacity: 0;
    transition: opacity var(--transition-fast);
  }

  /* Star toggle: hidden at rest, revealed on row hover / keyboard focus; a
	   starred message keeps it visible (filled, gold) like the feeds cards. */
  .star-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    padding: 0.1rem;
    color: var(--text-dim);
    cursor: pointer;
    opacity: 0;
    transition:
      opacity var(--transition-fast),
      color var(--transition-fast);
  }
  .msg:hover .star-btn,
  .cmd-row:hover .star-btn,
  .star-btn:focus-visible,
  .star-btn.starred {
    opacity: 1;
  }
  .star-btn:hover,
  .star-btn.starred {
    color: #f5b300;
  }

  /* Room label chip (aggregate views): a small clickable room tag in the
	   author header that jumps into the room. */
  .room-chip {
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-pill);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    line-height: 1.2;
    padding: 0.05rem 0.5rem;
    cursor: pointer;
    max-width: 12rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition:
      color var(--transition-fast),
      border-color var(--transition-fast);
  }
  .room-chip:hover {
    color: var(--text-primary);
    border-color: var(--text-dim);
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
  .avatar.bot {
    background: var(--accent-amber);
    color: #111;
  }

  .hover-time {
    font-size: 0.62rem;
    color: var(--text-dim);
    opacity: 0;
    line-height: 1.6;
    transition: opacity var(--transition-fast);
    font-variant-numeric: tabular-nums;
  }

  .content {
    flex: 1;
    min-width: 0;
  }

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
  .author.bot {
    color: var(--accent-amber);
  }
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
  .user-body {
    white-space: pre-wrap;
  }

  /* Activity-chip spacing. Base is flush (a tool-first turn puts the chip
	   directly under the meta, like a text answer). When a chip neighbours a
	   prose block it gets a paragraph-sized gap on that side so it doesn't crowd
	   the surrounding text. (ActivityTrace's own margin is 0 so this is the sole
	   source of vertical spacing.) */
  .chip-slot {
    margin: 0;
  }
  .chip-slot.gap-above {
    margin-top: 0.85rem;
  }
  .chip-slot.gap-below {
    margin-bottom: 0.85rem;
  }

  .msg.error .body,
  .cmd-output.error {
    color: #e0a0a0;
  }

  /* Command (!…) output: a left-aligned block set apart from the conversation
	   by a subtle card, so its lists / code / tables render left-to-right.
	   Position anchor for its own star bar (durable system rows in views). */
  .cmd-row {
    padding: 0.2rem 0.75rem 0.5rem;
    position: relative;
  }
  .cmd-row .room-chip {
    margin-bottom: 0.25rem;
  }
  .msg-actions.cmd-actions {
    top: 0.3rem;
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

  .attachments {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-top: 0.3rem;
  }
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
  .dot {
    flex: 0 0 auto;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--text-muted);
    animation: pulse 1.1s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,
    100% {
      opacity: 0.3;
    }
    50% {
      opacity: 1;
    }
  }

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
  :global(:root[data-theme='light']) .avatar.bot {
    color: #fff;
  }
</style>
