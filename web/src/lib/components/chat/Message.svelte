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
    active = false,
    touch = false,
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
    // Touch surrogate for hover: the one row the user last tapped. A touch
    // device has no hover to reveal the metadata + star with, and leaning on
    // Safari's synthesized :hover left the affordances stuck on every row
    // ever tapped. The list owns this so exactly one row can be active.
    active?: boolean;
    // True once the page's last pointer was a finger. `@media (hover: hover)`
    // answers for the device, not the gesture — a touchscreen laptop or an iPad
    // with a trackpad reports hover, and a tap there still strands a synthesized
    // :hover. This mutes the hover reveal for as long as touch is what's in use.
    touch?: boolean;
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

  // Under a finger the reveal is gated in the *markup*, not by opacity alone.
  // A control at zero opacity is still hit-testable, and the star sits at the
  // row's top-right: a tap that clipped it starred the message outright, and
  // because a tap on a button deliberately leaves the activation alone (see
  // tapActivation), the row never lit up either. What the user saw was a gold
  // star with no metadata beside it, on every row a thumb had brushed —
  // indistinguishable from the sticky-hover bug this was meant to have fixed,
  // and the tell was that only the star persisted while the metadata behaved.
  // A starred message keeps its star: that one is state, not an affordance.
  const revealed = $derived(!touch || active);
  const showMeta = $derived(revealed && meta.length > 0 && !message.streaming);
  const showStar = $derived(starrable && (revealed || !!message.starred));
  const hasActions = $derived(showStar || showMeta);
</script>

{#snippet starButton()}
  <button
    class="star-btn"
    class:starred={message.starred}
    onclick={(e) => {
      onToggleStar?.(message.cid);
      // A pointer-driven click leaves the button focused, and a focus ring that
      // also reveals the icon is a second way for a star to sit there lit after
      // the user has moved on (Safari has shipped :focus-visible on tap).
      // `detail > 0` is the pointer's signature — a keyboard activation reports
      // 0, so this can't take focus away from keyboard use.
      if (e.detail > 0) e.currentTarget.blur();
    }}
    aria-label={message.starred ? 'Unstar message' : 'Star message'}
    aria-pressed={message.starred ? 'true' : 'false'}
    title={message.starred ? 'Unstar' : 'Star'}
    type="button"
  >
    <Star size={14} fill={message.starred ? 'currentColor' : 'none'} />
  </button>
{/snippet}

<!-- Per-message metadata + actions: task id / model / duration / tool count,
     then the star. Rendered as the trailing member of the author header on a
     fresh group (so its text baseline-aligns with the timestamp for free), and
     absolutely positioned on a continuation row, which has no header. -->
{#snippet actionsBar()}
  <div class="msg-actions">
    {#if showMeta}
      <span class="meta-footer">{meta.join(' · ')}</span>
    {/if}
    {#if showStar}
      {@render starButton()}
    {/if}
  </div>
{/snippet}

{#if isSystem}
  <!-- Command (!…) output / delivered notifications. Left-aligned block, not a
	     centered notice: it carries lists / code / tables that must read
	     left-to-right. Durable system rows (msgId) are starrable too. -->
  <div
    class="cmd-row"
    class:active
    class:touch
    data-cid={message.cid}
    data-task-id={message.taskId ?? undefined}
  >
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
    {#if showStar}
      <div class="msg-actions cmd-actions">
        {@render starButton()}
      </div>
    {/if}
  </div>
{:else}
  <div
    class="msg"
    class:continuation
    class:active
    class:touch
    class:error={message.error}
    data-cid={message.cid}
    data-task-id={message.taskId ?? undefined}
  >
    <div class="gutter">
      {#if !continuation}
        <div class="avatar" class:bot={!isUser}>{initial}</div>
      {:else if revealed}
        <time class="hover-time">{time}</time>
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
          {#if hasActions}
            {@render actionsBar()}
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

    <!-- A continuation row has no author header to hang the bar off, so it
			     floats at the top-right, lined up with the gutter's hover time. -->
    {#if continuation && hasActions}
      {@render actionsBar()}
    {/if}
  </div>
{/if}

<style>
  /* Discord/Slack-style row: avatar gutter on the left, author + time header,
	   then the message body. Consecutive messages from the same author collapse
	   into one visual group (the `.continuation` rows hide the header). */
  .msg {
    display: flex;
    /* Tokenised because on mobile it is load-bearing: the row's inline padding,
		   the gutter and this gap together decide where the message text starts, and
		   that has to match the headings above it. See app.css. */
    gap: var(--chat-avatar-gap);
    /* Extra bottom padding so the hover highlight isn't flush with the last
		   line of text. */
    padding: 0.1rem var(--chat-row-inline) 0.45rem;
    align-items: flex-start;
    /* Anchor for the absolutely-positioned .meta-footer (top-right). */
    position: relative;
  }
  .msg:not(.continuation) {
    margin-top: 0.7rem;
    padding-top: 0.45rem;
  }
  /* Reveal rules. With a real pointer the row's own :hover drives them; under a
	   finger the list marks a single `.active` row instead. Splitting them
	   matters: iOS Safari synthesizes :hover on tap and clears it only when a
	   later tap displaces it, so an unguarded :hover left a star showing on every
	   row the user had ever tapped. `.active` is the touch surrogate and is
	   inherently single. Two guards, because they fail on different devices — the
	   media query knows a phone has no hover at all, `.touch` knows a finger was
	   used on a device that also has a mouse. */
  @media (hover: hover) {
    .msg:not(.touch):hover .hover-time,
    .msg:not(.touch):hover .meta-footer {
      opacity: 1;
    }
  }
  .msg.active .hover-time,
  .msg.active .meta-footer {
    opacity: 1;
  }

  /* Per-message actions bar: hover metadata + the star toggle. One bar so the
		   two hover surfaces can't collide. Where it sits depends on whether the row
		   has an author header, because it must line up with that row's timestamp —
		   and the timestamp lives in two different places. */
  .msg-actions {
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Fresh group: the bar is the trailing member of the .meta header, so the
		   shared `align-items: baseline` puts its text on the timestamp's baseline by
		   construction. A hand-tuned offset can't do that — it has to hold across font
		   metrics that differ per platform, and it drifted on iOS Safari. */
  .meta .msg-actions {
    margin-left: auto;
    align-self: baseline;
    /* Yield to the author/time rather than pushing them out of the row. */
    min-width: 0;
  }
  /* The star is an icon button with no text baseline of its own; centre it on
		   the bar instead of letting it hang off the synthesized one. */
  .meta .msg-actions .star-btn {
    align-self: center;
  }
  /* Continuation: no header, so float it top-right against the gutter's
		   .hover-time. `top` matches the gutter's own padding, and the two share a
		   font-size + line-height (below), so their line boxes — and therefore their
		   baselines — coincide without a magic offset. */
  .msg.continuation .msg-actions {
    position: absolute;
    right: var(--chat-row-inline);
    top: 0.1rem;
  }

  /* Subtle per-message metadata, revealed on hover (child of the actions bar). */
  .meta-footer {
    font-size: var(--text-xs);
    line-height: 1.6;
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    /* A narrow row trims the tail (tool count, then duration) rather than
			   squeezing the author name — the id and model are the identifying bits. */
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0;
    transition: opacity var(--transition-fast);
  }

  /* Star toggle: hidden at rest, revealed on row hover (or tap-activation on
	   touch) / keyboard focus; a starred message keeps it visible (filled, gold)
	   like the feeds cards.

	   The fade is a pointer-device affordance only — see the `.touch` rule at the
	   end of this block for why it is switched off under a finger. */
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
    /* Hidden *and* inert. Opacity alone leaves the button hit-testable, so a
		   tap landing on the invisible star starred the message with nothing on
		   screen to explain it. The markup gate above covers the touch path once a
		   finger has been seen; this covers the frame before that and any pointer
		   device where the row renders the button unrevealed. Keyboard focus is
		   unaffected — pointer-events does not gate Tab — and :focus-visible below
		   hands interactivity back. */
    pointer-events: none;
    transition:
      opacity var(--transition-fast),
      color var(--transition-fast);
  }
  @media (hover: hover) {
    .msg:not(.touch):hover .star-btn,
    .cmd-row:not(.touch):hover .star-btn {
      opacity: 1;
      pointer-events: auto;
    }
    .msg:not(.touch) .star-btn:hover,
    .cmd-row:not(.touch) .star-btn:hover {
      color: var(--accent-amber);
    }
  }
  .msg.active .star-btn,
  .cmd-row.active .star-btn,
  .star-btn:focus-visible,
  .star-btn.starred {
    opacity: 1;
    pointer-events: auto;
  }
  .star-btn.starred {
    color: var(--accent-amber);
  }

  /* Under a finger the reveal is a swap, not a fade — for both affordances, so
	   there is one rule rather than a fade here and an on/off there.

	   Not cosmetic. An opacity transition is what asks the compositor to promote
	   an element to its own layer, and a promoted layer whose opacity returns to
	   0 without being repainted keeps showing what it last painted: the star
	   stranded on every row a thumb had tapped, while the plain text span beside
	   it — never promoted — cleared correctly. That asymmetry is what identified
	   the mechanism. The markup gate above does not on its own avoid this: the
	   node is inserted and the row's .active class lands in an order that leaves
	   a style change to animate, so a transition really does run on the touch
	   path (measured: one frame after a tap, opacity 0 with a transition in
	   flight). Keyed on .touch and not a width breakpoint, because the axis is
	   what the user's hand is doing — an iPad is wide and touch, a narrow
	   desktop window is neither. */
  .msg.touch .star-btn,
  .cmd-row.touch .star-btn,
  .msg.touch .meta-footer,
  .msg.touch .hover-time {
    transition: none;
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
    flex: 0 0 var(--chat-gutter);
    display: flex;
    justify-content: center;
    padding-top: 0.1rem;
  }
  .avatar {
    width: var(--chat-avatar);
    height: var(--chat-avatar);
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
    background: var(--accent-amber-fill);
    color: var(--accent-amber-fill-fg);
  }

  /* The mobile avatar is a good deal smaller (it is what buys the shared text
	   inset — see app.css), so the initial and the corner both have to come down
	   with it: the desktop values leave a 600-weight glyph crowding the box, and
	   a 0.5rem radius on a 1.25rem square is a pill rather than a rounded square. */
  @media (max-width: 768px) {
    /* The avatar is narrower than its column here (the column's width is fixed
		   by the shared text inset, the avatar's by the sigil it lines up with), so
		   it hugs the leading edge instead of centring in the leftover. */
    .gutter {
      justify-content: flex-start;
    }

    .avatar {
      font-size: var(--text-xs);
      border-radius: 0.3rem;
    }

    /* The continuation-row stamp shares that column, and a `06:25 PM` does not
		   fit 1.25rem — centred in the gutter it would overhang the row's left edge
		   and spill ~9px into the message text on hover. Drop it here rather than
		   shrink it (no size makes it fit): it is a hover affordance, and this
		   breakpoint is overwhelmingly touch, where it never appears anyway. The
		   time is still on the group header above, and the floating actions bar is
		   positioned independently of it. */
    .hover-time {
      display: none;
    }
  }

  /* Continuation-row timestamp. Font-size and line-height are deliberately the
	   same as .meta-footer's so the two line boxes match and the floating actions
	   bar lands on this baseline exactly. */
  .hover-time {
    font-size: var(--text-xs);
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
    /* Author and time hold their size; the metadata bar is what gives way when
		   the header row runs out of width. */
    flex-shrink: 0;
  }
  .author.bot {
    color: var(--accent-amber);
  }
  .stamp {
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
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
  /* A tool-first turn opens with a chip directly under the author header. Flush
	   reads cramped against the header, a full paragraph gap reads detached — so
	   it gets half the neighbour gap. */
  .meta + .chip-slot {
    margin-top: 0.425rem;
  }

  .msg.error .body,
  .cmd-output.error {
    color: var(--status-danger-fg);
  }

  /* Command (!…) output: a left-aligned block set apart from the conversation
	   by a subtle card, so its lists / code / tables render left-to-right.
	   Position anchor for its own star bar (durable system rows in views). */
  .cmd-row {
    padding: 0.2rem var(--chat-row-inline) 0.5rem;
    position: relative;
  }
  .cmd-row .room-chip {
    margin-bottom: 0.25rem;
  }
  /* A command row has neither header nor gutter, so its star floats top-right
	   on its own. (The bar is only absolute here and on continuation rows.) */
  .msg-actions.cmd-actions {
    position: absolute;
    right: var(--chat-row-inline);
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
</style>
