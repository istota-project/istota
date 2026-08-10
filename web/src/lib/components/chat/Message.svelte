<script lang="ts">
  import { Copy, Star, Trash2, Reply } from 'lucide-svelte';
  import { chatFileUrl } from '$lib/api';
  import { copyText } from '$lib/clipboard';
  import { renderMarkdown } from '$lib/markdown';
  import type { ChatMessage } from '$lib/stores/chat';
  import { messageCopyText, renderGroups } from '$lib/stores/segments';
  import { Button } from '$lib/components/ui';
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
    onDelete,
    onReply,
    onJumpToMessage,
    onRetry,
    retryBusy = false,
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
    // Delete a durable message. The handler owns the confirmation — this
    // component only offers the affordance. Absent → no delete affordance.
    onDelete?: (cid: number) => void;
    // Stage a reply citing this message. The handler owns the composer chip —
    // this component only offers the affordance. Absent → no reply affordance,
    // which is also how the aggregate panes stay read-only.
    onReply?: (cid: number) => void;
    // Follow this turn's own citation back to the message it names. Absent →
    // the quote block renders but doesn't click through.
    onJumpToMessage?: (msgId: number) => void;
    // Re-send a message whose send failed. Absent → the failure is reported
    // without an offer to retry it (read-only surfaces, aggregate views).
    onRetry?: (cid: number) => void;
    // True while the room has a turn in flight. Retry is refused then (the
    // store's `runTurn` is not re-entrant), so the button says so rather than
    // silently doing nothing.
    retryBusy?: boolean;
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
  // A user row is not always the viewer's own words — a shared room has other
  // members, and an email mirrored into the room it continues was written by
  // whoever sent it. The server names them when it can; `userName` is the
  // fallback, and stays right for everything the viewer typed here.
  const author = $derived(isUser ? (message.author ?? userName) : botName);
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

  // Copy is per *turn*, in a row under the message, alongside delete.
  //
  // It used to hang off each prose block, on the theory that you lift one
  // paragraph out of an answer rather than the whole thing. In practice the
  // opposite is true — a reply is usually taken whole — and the per-block
  // version put a second, differently-placed affordance on a surface that
  // already had one (the star), which is what made adding delete the moment to
  // collapse them. `messageCopyText` keeps the property the per-block version
  // was really protecting: activity chips are excluded, so a tool trace still
  // never reaches the clipboard.
  const copySource = $derived(messageCopyText(message));
  // Withheld while streaming: copying a half-written turn hands back half an
  // answer, and the row would sit under text that is still moving.
  const showCopy = $derived(!message.streaming && !!copySource.trim());
  // Delete needs a durable row — a live placeholder isn't stored yet — and a
  // handler willing to confirm it.
  const showDelete = $derived(typeof message.msgId === 'number' && !!onDelete);
  // Star appears twice on a turn, and the two are not redundant. The hover bar's
  // is the one that *persists* at rest on a starred row, which is what makes a
  // starred message legible without hovering it; this one is where the hand
  // already is once the row's actions are open, next to the other two things you
  // do to a whole turn. Same condition as the bar's, so they can't disagree
  // about whether the turn is starrable.
  const showRowStar = $derived(starrable);
  // Reply needs a durable id to cite — the same rule star and delete follow,
  // which correctly withholds it from optimistic rows and in-flight
  // placeholders — and somewhere to stage into. The aggregate panes have no
  // composer, so a staged reply there would have nowhere to go.
  const showReply = $derived(typeof message.msgId === 'number' && !!onReply && !aggregate);
  // The parent this turn cites. `deleted` is truthy-tested, never compared to
  // false: a citation staged in the composer carries no flag at all, and
  // treating absence as deleted would render every fresh reply muted.
  const cited = $derived(message.replyTo);
  const citedDeleted = $derived(!!cited?.deleted);
  const citedLabel = $derived(
    cited?.role === 'user' ? userName : cited?.role === 'assistant' ? botName : '',
  );
  const citedClickable = $derived(!!cited && !citedDeleted && !!onJumpToMessage);
  // ---- Send lifecycle (ISSUE-200) -------------------------------------------
  // A send that failed reports on the message that failed, not on an assistant
  // placeholder standing in for a reply that was never attempted.
  const sendFailed = $derived(message.sendState === 'failed');
  // Truthful state is set the moment the row exists; the store's grace timer
  // opens `showSending` only once the send is slow enough to be worth saying.
  const sendPending = $derived(message.sendState === 'sending' && !!message.showSending);
  // `retryable` is false where a retry would fail identically (an expired
  // session), and an offer that cannot work is worse than no offer.
  const showRetry = $derived(sendFailed && message.retryable !== false && !!onRetry);

  // The row is in the layout whenever any of the three could be there, so
  // revealing it never reflows the transcript under the pointer. Withheld
  // entirely from a failed send: all three act on a durable turn, and this one
  // never became one — star and delete have no `msgId` to work with, and a lone
  // copy button would compete with the Retry that is the actual next move.
  const hasRowActions = $derived(
    (showCopy || showRowStar || showReply || showDelete) && !sendFailed,
  );
</script>

<!-- Turn-level actions, left-aligned under the message body. In the flow
     rather than absolutely positioned: it sits below the content it acts on,
     so it needs to take the space it occupies — the old per-block button was
     absolute because it overlapped the block's own bottom padding. `revealed`
     gates opacity only; the row keeps its box either way, so nothing shifts
     when it appears. -->
{#snippet turnActions()}
  <div class="turn-actions" class:revealed>
    {#if showCopy}
      <button
        class="turn-action"
        onclick={(e) => {
          void copyText(copySource, { label: 'Copied' });
          // Same reason as the star: a pointer click leaves the button
          // focused, and a focus ring that also reveals it is a second way for
          // it to sit there lit after the user has moved on. A keyboard
          // activation reports detail 0, so this can't take focus away from
          // keyboard use.
          if (e.detail > 0) e.currentTarget.blur();
        }}
        aria-label="Copy message"
        title="Copy"
        type="button"
      >
        <Copy size={15} />
      </button>
    {/if}
    {#if showRowStar}
      <!-- Between copy and delete: the row then reads left to right in
           ascending consequence, and the destructive button ends up at the end
           of the row rather than immediately beside the benign one. -->
      <button
        class="turn-action star"
        class:starred={message.starred}
        onclick={(e) => {
          onToggleStar?.(message.cid);
          if (e.detail > 0) e.currentTarget.blur();
        }}
        aria-label={message.starred ? 'Unstar message' : 'Star message'}
        aria-pressed={message.starred ? 'true' : 'false'}
        title={message.starred ? 'Unstar' : 'Star'}
        type="button"
      >
        <Star size={15} fill={message.starred ? 'currentColor' : 'none'} />
      </button>
    {/if}
    {#if showReply}
      <!-- After star, before delete. The row reads left to right in ascending
           consequence: reply stages a new message, which is more than a
           private mark and less than a destructive removal — and keeping
           delete last leaves it terminal. -->
      <button
        class="turn-action"
        onclick={(e) => {
          onReply?.(message.cid);
          if (e.detail > 0) e.currentTarget.blur();
        }}
        aria-label="Reply to message"
        title="Reply"
        type="button"
      >
        <Reply size={15} />
      </button>
    {/if}
    {#if showDelete}
      <button
        class="turn-action danger"
        onclick={(e) => {
          onDelete?.(message.cid);
          if (e.detail > 0) e.currentTarget.blur();
        }}
        aria-label="Delete message"
        title="Delete"
        type="button"
      >
        <Trash2 size={15} />
      </button>
    {/if}
  </div>
{/snippet}

<!-- The citation, above the body it belongs to. Rendered from the durable
     store, so it survives a reload, a room switch and the retention sweep that
     deletes the task. A live parent clicks through; a deleted one says so and
     stays inert — the deletion is a fact about the conversation, and dropping
     the citation would rewrite it. -->
{#snippet replyQuote()}
  {#if cited}
    {#if citedClickable}
      <button
        class="reply-quote"
        class:under-meta={!continuation}
        onclick={(e) => {
          onJumpToMessage?.(cited.msgId);
          if (e.detail > 0) e.currentTarget.blur();
        }}
        title="Go to the message this replies to"
        type="button"
      >
        {#if citedLabel}<span class="reply-quote-author">{citedLabel}</span>{/if}
        <span class="reply-quote-text">{cited.excerpt ?? ''}</span>
      </button>
    {:else}
      <div class="reply-quote" class:under-meta={!continuation} class:deleted={citedDeleted}>
        {#if citedDeleted}
          <span class="reply-quote-text">Original message deleted</span>
        {:else}
          {#if citedLabel}<span class="reply-quote-author">{citedLabel}</span>{/if}
          <span class="reply-quote-text">{cited.excerpt ?? ''}</span>
        {/if}
      </div>
    {/if}
  {/if}
{/snippet}

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
      <div class="cmd-output markdown" class:error={message.error}>
        {@html bodyHtml}
      </div>
    {/if}
    {#if showStar}
      <div class="msg-actions cmd-actions">
        {@render starButton()}
      </div>
    {/if}
    <!-- A search-results row renders cards, not markdown, so there is no
         source worth copying and no durable body to remove. -->
    {#if hasRowActions && !message.searchResults}
      {@render turnActions()}
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

      {@render replyQuote()}

      {#if isUser}
        {#if message.text}
          <!-- The text carries `pre-wrap`, so it needs its own element: with
               the whitespace rule on the wrapper, the newlines and indentation
               around a sibling button would render as leading and trailing
               blank space in every user message. -->
          <div class="body user-body">
            <span class="user-text">{message.text}</span>
          </div>
        {/if}
        {#if message.attachments?.length}
          <div class="attachments">
            {#each message.attachments as name, i}
              {@const href = message.attachmentPaths?.[i]}
              <!-- A chip is a link only when the file endpoint can serve it to
							     this user (their own workspace). Anything else — a co-member's
							     upload, a deployment with no local workspace — stays the inert
							     label it was, rather than becoming a link that 403s. -->
              {#if href}
                <a class="attachment attachment-link" href={chatFileUrl(href)} download={name}>
                  📎 {name}
                </a>
              {:else}
                <span class="attachment">📎 {name}</span>
              {/if}
            {/each}
          </div>
        {/if}
        <!-- The send's own state, on the message it belongs to. A failure here
             used to be written into the assistant placeholder, which read as
             "the reply failed" rather than "your message never left". -->
        {#if sendPending}
          <div class="progress send-pending">
            <span class="dot"></span>
            <span class="status-text">Sending…</span>
          </div>
        {:else if sendFailed}
          <div class="send-failed">
            <span class="send-failed-text">{message.sendError || 'Couldn’t send.'}</span>
            {#if showRetry}
              <Button
                variant="subtle"
                size="sm"
                disabled={retryBusy}
                title={retryBusy ? 'Wait for the current turn to finish' : undefined}
                onclick={() => onRetry?.(message.cid)}
              >
                Retry
              </Button>
            {/if}
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
            <div class="body markdown">
              {@html renderMarkdown(g.text)}
            </div>
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

      {#if hasRowActions}
        {@render turnActions()}
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
    padding: 0.1rem var(--chat-row-inline) var(--space-2);
    align-items: flex-start;
    /* Anchor for the absolutely-positioned .meta-footer (top-right). */
    position: relative;
  }
  /* A fresh author group separates itself with padding alone. It also carried a
	   `--space-3` top margin, which stacked with this row's own bottom padding and
	   the previous row's — three sources of gap for one boundary, and by far the
	   largest. Padding rather than margin is what the row wants anyway: the hover
	   highlight spans the padding box, so gap expressed as margin is a dead strip
	   between two rows that neither one lights up. */
  .msg:not(.continuation) {
    padding-top: var(--space-2);
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
    gap: var(--space-2);
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
  .msg.touch .hover-time,
  .msg.touch .turn-actions,
  .cmd-row.touch .turn-actions {
    transition: none;
  }

  /* Turn-level action row: copy + delete, left-aligned under the message body.
	   One row per turn rather than a button per block — see the script block for
	   why that moved.

	   Unlike the star, this is *in the flow*: it sits below the content it acts
	   on, so it has to take the space it occupies or revealing it would push the
	   next message down. The row is therefore always in the layout and only its
	   opacity is gated, which is also what lets the buttons be bare icons with
	   no background — they never overlap text.

	   `pointer-events` follows opacity for the same reason the star's does: a
	   control at zero opacity is still hit-testable, and a delete button a thumb
	   can hit without seeing is the worst version of that bug. */
  .turn-actions {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    margin-top: var(--space-1);
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--transition-fast);
  }
  /* Two guards, as everywhere else in this file: the media query knows a phone
	   has no hover at all, `.touch` knows a finger was used on a device that also
	   reports one. `.revealed` is the touch surrogate the row already computes. */
  @media (hover: hover) {
    .msg:not(.touch):hover .turn-actions,
    .cmd-row:not(.touch):hover .turn-actions {
      opacity: 1;
      pointer-events: auto;
    }
  }
  .msg.active .turn-actions.revealed,
  .cmd-row.active .turn-actions.revealed,
  .turn-actions:focus-within {
    opacity: 1;
    pointer-events: auto;
  }
  .turn-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: var(--space-1);
    background: none;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-dim);
    font: inherit;
    cursor: pointer;
    transition:
      color var(--transition-fast),
      background var(--transition-fast);
  }
  .turn-action:hover {
    color: var(--text-primary);
    background: var(--surface-raised);
  }
  .turn-action.danger:hover {
    color: var(--status-danger-fg);
  }
  /* The starred colour is state, so it holds without hover — but the row it
	   sits in is itself revealed on hover, so this never shows on a resting row.
	   The hover-bar star is the one that persists at rest; see the script block. */
  .turn-action.star.starred,
  .turn-action.star:hover {
    color: var(--accent-amber);
  }
  /* Touch targets, as an out-of-flow overlay so reaching them costs the row no
	   height (SidebarToggle's device).

	   The full 44px is only taken vertically. Horizontally the overlay is the
	   button plus one gap, so two adjacent overlays meet exactly at the gap's
	   midpoint: a tap in the seam resolves to the side it actually fell on,
	   rather than to whichever won the stacking order. Two 44px-wide overlays
	   would need ~21px between these buttons to stay apart, which is far wider
	   than two adjacent icons should sit — so the width is what gives, and it is
	   derived from the gap rather than restated, or tightening one would silently
	   reintroduce the overlap. */
  @media (max-width: 768px) {
    .turn-actions {
      --turn-action-gap: var(--space-2);
      gap: var(--turn-action-gap);
    }
    .turn-action {
      position: relative;
    }
    .turn-action::before {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      width: calc(100% + var(--turn-action-gap));
      height: 44px;
      transform: translate(-50%, -50%);
    }
  }

  /* The citation, above the body it belongs to. A quiet card with a leading
	   rule, so it reads as something quoted rather than as part of the message.
	   One rule set for both the clickable <button> and the inert <div>, since
	   the two differ only in whether they respond.

	   Geometry is the activity chip's, because the quote sits in the same slot —
	   a block between the author header and the turn's content. So: the body's
	   width cap, and `gap-below`'s margin, the block beneath being prose on
	   every path but an attachment-only turn, where it is the chips. */
  .reply-quote {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    width: 100%;
    max-width: var(--chat-body-max);
    margin-bottom: var(--space-3);
    padding: var(--space-1) var(--space-2);
    background: var(--surface-card);
    border: none;
    border-left: 2px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    line-height: 1.4;
    text-align: left;
    cursor: pointer;
  }
  /* Under the author header, the same half gap `.meta + .chip-slot` takes:
	   flush reads cramped against the header, a full paragraph gap reads
	   detached. On a continuation row there is no header and the base rule's
	   flush top is right, exactly as it is for a tool-first chip.

	   Written as a class on the element rather than as `.meta + .reply-quote`,
	   because the condition is `!continuation` — the same variable that decides
	   whether the header renders at all — and not a DOM adjacency that happens
	   to follow from it. It is also the half a jsdom test can see, the sibling
	   selector being reachable only through the cascade. */
  .reply-quote.under-meta {
    margin-top: calc(var(--space-3) / 2);
  }
  .reply-quote:is(div) {
    cursor: default;
  }
  button.reply-quote:hover {
    border-left-color: var(--link);
    color: var(--text-secondary);
  }
  .reply-quote.deleted {
    font-style: italic;
    color: var(--text-dim);
  }
  .reply-quote-author {
    flex: 0 0 auto;
    color: var(--text-dim);
  }
  /* One line: the quote points at a message, it does not reproduce it. */
  .reply-quote-text {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
    padding: 0.05rem var(--space-2);
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
    border-radius: var(--radius-md);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.85rem;
    font-weight: 600;
    /* design-lint-allow-begin: fixed surface — an avatar fill is an identity
       chip, not a themed surface, so it holds one value in both themes. */
    color: #fff;
    background: #4a4a52;
    /* design-lint-allow-end */
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
      border-radius: var(--radius-sm);
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
    gap: var(--space-2);
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
    max-width: var(--chat-body-max);
  }
  /* On the inner span, not the wrapper: the wrapper also holds the copy
	   button, and under `pre-wrap` the markup whitespace around that button
	   would render as real blank space around the message text. */
  .user-text {
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
  /* A chip's preceding neighbour is always a prose block (tool runs coalesce,
	   so chips never abut), and this margin is the whole of the separation
	   between them. It was briefly cut to a hairline while each block reserved a
	   strip below its text for a per-block copy button; that reserve is gone
	   with the button, so it is back to matching `gap-below`. */
  .chip-slot.gap-above {
    margin-top: var(--space-3);
  }
  .chip-slot.gap-below {
    margin-bottom: var(--space-3);
  }
  /* A tool-first turn opens with a chip directly under the author header. Flush
	   reads cramped against the header, a full paragraph gap reads detached — so
	   it gets half the neighbour gap. Derived rather than written out: it was
	   0.425rem, half of the 0.85rem `gap-below` used to be, and that half went
	   stale the moment the neighbour moved onto the scale. */
  .meta + .chip-slot {
    margin-top: calc(var(--space-3) / 2);
  }

  .msg.error .body,
  .cmd-output.error {
    color: var(--status-danger-fg);
  }

  /* Send lifecycle on the user's own row (ISSUE-200). Both marks sit under the
	   message body, where the turn-action row would be — the send has to settle
	   before that row has anything to act on. */
  .send-pending {
    margin-top: var(--space-1);
  }
  .send-failed {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-1);
    font-size: var(--text-sm);
    color: var(--status-danger-fg);
  }
  .send-failed-text {
    min-width: 0;
  }

  /* Command (!…) output: a left-aligned block set apart from the conversation
	   by a subtle card, so its lists / code / tables render left-to-right.
	   Position anchor for its own star bar (durable system rows in views). */
  .cmd-row {
    padding: 0.2rem var(--chat-row-inline) 0.5rem;
    position: relative;
  }
  .cmd-row .room-chip {
    margin-bottom: var(--space-1);
  }
  /* A command row has neither header nor gutter, so its star floats top-right
	   on its own. (The bar is only absolute here and on continuation rows.) */
  .msg-actions.cmd-actions {
    position: absolute;
    right: var(--chat-row-inline);
    top: 0.3rem;
  }
  .cmd-output {
    max-width: var(--chat-body-max);
    font-size: var(--text-sm);
    line-height: 1.5;
    color: var(--text-secondary);
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3);
    text-align: left;
    word-break: break-word;
  }

  .attachments {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    margin-top: var(--space-1);
  }
  .attachment {
    font-size: var(--text-xs);
    color: var(--text-muted);
    background: var(--surface-base);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-pill);
    padding: 0.1rem var(--space-2);
  }
  .attachment-link {
    text-decoration: none;
    cursor: pointer;
  }
  .attachment-link:hover,
  .attachment-link:focus-visible {
    color: var(--text-primary);
    border-color: var(--border-hover);
  }

  .progress {
    display: flex;
    align-items: center;
    gap: var(--space-2);
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
