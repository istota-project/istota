<script lang="ts">
  /**
   * One outbound email the approval gate is holding, and the surface it is
   * answered from.
   *
   * Larger than `ConfirmationCard` on purpose: what is being approved is a
   * specific set of bytes going to a specific set of addresses, so the card
   * renders the drafted body and the whole recipient list rather than a
   * summary. The single promise this feature makes is that approving sends
   * exactly what was read, which is a promise about *transformation*, not about
   * how much is on screen at once: the body is a text node under `pre-wrap` and
   * is never re-rendered, and `Send` and `Edit` both operate on the full stored
   * `body` — never on `shownBody`. Collapsing a long body behind an expander is
   * therefore a reading affordance and not an abbreviation of what is sent.
   *
   * `placement` is which of the two slots the card is in, and it sets how much
   * body shows before the expander. `turn` is inline under the assistant row
   * that composed the mail, where the reader arrived by scrolling to it and the
   * body is the thing they came for. `banner` is the page's own list above the
   * transcript, which is pinned chrome carrying drafts whose turn is not on
   * screen — there the card is competing with the transcript for the top of the
   * pane, so it previews a line or two and expands on request.
   *
   * Three shapes, checked in this order, because the later ones assume content
   * the earlier ones do not have:
   *
   * 1. `unreadable` — a stored column does not parse. Discard is the only
   *    action offered; approving is refused server-side anyway, and editing a
   *    body whose recipients we cannot read is worse than not offering it.
   * 2. `status === 'sending'` — the row was claimed and the process did not get
   *    back to finalize it. Deliberately terminal: nobody can know whether the
   *    mail went out, so the card says to check the Sent folder and offers
   *    nothing. Every other producer filters this status away, which is why it
   *    was invisible before this stage.
   * 3. `truncated` — the stream frame spent its byte budget; the full row comes
   *    from `GET /chat/drafts`, which the card asks for on mount.
   *
   * **Everything here is a text node.** The subject and body are model-composed
   * from a thread with a stranger, and the recipients are addresses off that
   * thread. Rendering any of it as markup is the injection this whole feature
   * exists to make harder, not easier.
   */
  import { Button } from '$lib/components/ui';
  import { Mail } from 'lucide-svelte';
  import type { OutboundDraft } from '$lib/api';

  let {
    draft,
    onApprove,
    onDiscard,
    onEdit,
    onNeedsFullRow,
    placement = 'turn',
  }: {
    draft: OutboundDraft;
    onApprove: (id: number) => Promise<boolean> | boolean;
    onDiscard: (id: number) => Promise<boolean> | boolean;
    onEdit: (id: number, body: string) => Promise<boolean> | boolean;
    /** A stub arrived on the stream; ask for the full set. */
    onNeedsFullRow?: () => void;
    /** Which slot this card is in. See the module comment. */
    placement?: 'turn' | 'banner';
  } = $props();

  let busy = $state(false);
  let editing = $state(false);
  let editBody = $state('');
  let expanded = $state(false);

  const unreadable = $derived(draft.unreadable === true);
  const sending = $derived(draft.status === 'sending');
  const truncated = $derived(draft.truncated === true);
  const body = $derived(draft.body ?? '');
  const recipients = $derived([...(draft.to ?? []), ...(draft.cc ?? []), ...(draft.bcc ?? [])]);
  const actions = $derived(draft.actions_taken ?? []);
  // Long bodies collapse to a readable preview with expand-in-place. The cut is
  // on the rendered text, never on what gets sent — `onEdit` posts `editBody`,
  // which is seeded from the full `body`.
  const PREVIEW_CHARS = 700;
  const longBody = $derived(body.length > PREVIEW_CHARS);
  // The ellipsis is folded in here rather than written beside the interpolation
  // in the markup: the block is `white-space: pre-wrap`, so a second expression
  // beside it lets the formatter break the line and turn its indentation into
  // rendered leading whitespace on the message being approved.
  // `slice` cuts on UTF-16 code units, so a cut landing between the halves of a
  // surrogate pair leaves a lone high surrogate and renders U+FFFD. On a card
  // whose whole claim is that what you read is what is sent, a replacement
  // character reads as the message itself being damaged.
  const preview = $derived(
    /[\uD800-\uDBFF]$/.test(body.slice(0, PREVIEW_CHARS))
      ? body.slice(0, PREVIEW_CHARS - 1)
      : body.slice(0, PREVIEW_CHARS),
  );
  const shownBody = $derived(expanded || !longBody ? body : `${preview}…`);

  // ---- The banner's compact form ------------------------------------------
  // Shortening the preview was not enough on its own: a body carrying a blank
  // line still renders as two paragraphs under `pre-wrap`, and the fields grid,
  // the actions list and the button row are each a row of their own — so two
  // held drafts still took most of the pane. Compact is a different *shape*,
  // not a shorter one: a head line, recipients and subject on one line, one
  // clipped line of body, and the buttons. Four rows, and it expands to the
  // full card in place.
  //
  // Editing is excluded because the textarea is the full-height surface the
  // whole card exists to offer; there is no compact way to write a reply.
  const compact = $derived(placement === 'banner' && !expanded && !editing);
  // Whitespace is collapsed rather than preserved, because this line is a
  // *label* for the held message and not the message itself — the paragraph
  // breaks are exactly what made 180 characters occupy three rows. Clipping is
  // CSS (`text-overflow: ellipsis`), so the line fits whatever width the card
  // has instead of guessing a character count that is wrong at one of them.
  // The full text is one click away and is what `Send` and `Edit` operate on.
  const peek = $derived(body.replace(/\s+/g, ' ').trim());
  const summaryLine = $derived(
    [recipients.join(', ') || '(none)', draft.subject || '(no subject)'].join(' · '),
  );

  // A stub carries no body, so the card cannot render what is being approved.
  // Asking on arrival is what turns it back into a full row.
  //
  // The latch is cleared the moment the row *stops* being truncated, rather
  // than being a once-per-instance flag. The stream frame is diffed against the
  // server's own baseline, which the client's refetch does not touch, so the
  // same draft is stubbed again on the next frame that changes anything — with
  // a permanent latch the card then sat on "Loading the held message…" forever,
  // offering no action on mail that was waiting to be answered.
  let asked = $state(false);
  $effect(() => {
    if (!truncated) {
      asked = false;
      return;
    }
    if (!asked) {
      asked = true;
      onNeedsFullRow?.();
    }
  });

  const reasonLabel = $derived(
    draft.hold_reason === 'all_mode'
      ? 'every outbound message needs approval'
      : 'a recipient is not on your trusted list',
  );

  async function run(fn: () => Promise<boolean> | boolean) {
    if (busy) return;
    busy = true;
    try {
      await fn();
    } finally {
      busy = false;
    }
  }

  function startEdit() {
    editBody = body;
    editing = true;
  }

  async function saveEdit() {
    const next = editBody;
    await run(async () => {
      const ok = await onEdit(draft.id, next);
      if (ok) editing = false;
      return ok;
    });
  }
</script>

<article class="draft-card" class:attention={sending || unreadable} aria-label="Held email">
  {#if unreadable}
    <div class="draft-head">
      <span class="micro-label">Held email &middot; unreadable</span>
    </div>
    <p class="draft-note">
      Draft #{draft.id} is held but its stored record could not be read, so it cannot be shown or sent.
      Discarding it is safe — nothing has been delivered.
    </p>
    <div class="draft-actions">
      <Button
        variant="subtle"
        size="sm"
        disabled={busy}
        onclick={() => run(() => onDiscard(draft.id))}>Discard</Button
      >
    </div>
  {:else if truncated}
    <!-- Ahead of the `sending` branch, not after it: a stub carries a status
         but no subject, so a stuck row arriving as a stub would render
         "(no subject)" — asserting the message has none on the very card whose
         job is to help find it in the Sent folder. -->
    <div class="draft-head">
      <span class="micro-label">Held email</span>
    </div>
    <p class="draft-note">Loading the held message…</p>
  {:else if sending}
    <div class="draft-head">
      <span class="micro-label">Held email &middot; sending</span>
    </div>
    <p class="draft-line">{draft.subject || '(no subject)'}</p>
    <p class="draft-note">
      This message was approved and the send did not report back, so it may already have gone out.
      Check your Sent folder before resending it.
    </p>
  {:else if compact}
    <!-- The banner's four rows. Everything withheld here is one click away and
         nothing is withheld from what gets sent: `Send` and `Edit` read the
         full stored body, exactly as they do on the expanded card. -->
    <div class="draft-head">
      <span class="micro-label"><Mail size={12} /> Held for approval</span>
      <span class="caption">{reasonLabel}</span>
    </div>
    <p class="draft-summary">{summaryLine}</p>
    <p class="draft-peek">{peek}</p>
    <div class="draft-actions">
      <Button
        variant="primary"
        size="sm"
        disabled={busy}
        onclick={() => run(() => onApprove(draft.id))}>Send</Button
      >
      <Button variant="secondary" size="sm" disabled={busy} onclick={startEdit}>Edit</Button>
      <Button
        variant="subtle"
        size="sm"
        disabled={busy}
        onclick={() => run(() => onDiscard(draft.id))}>Discard</Button
      >
      <!-- In the actions row rather than under the body: it saves the card a
           whole row, and it is where the hand already is. -->
      <button class="draft-more" type="button" onclick={() => (expanded = true)}>
        Show the whole message
      </button>
    </div>
  {:else}
    <div class="draft-head">
      <span class="micro-label"><Mail size={12} /> Held for approval</span>
      <span class="caption">{reasonLabel}</span>
      {#if placement === 'banner'}
        <button class="draft-more" type="button" onclick={() => (expanded = false)}>
          Show less
        </button>
      {/if}
    </div>

    <dl class="draft-fields kv">
      <dt>To</dt>
      <dd>{recipients.join(', ') || '(none)'}</dd>
      <dt>Subject</dt>
      <dd>{draft.subject || '(no subject)'}</dd>
      {#if draft.attachments?.length}
        <dt>Attached</dt>
        <dd>{draft.attachments.join(', ')}</dd>
      {/if}
    </dl>

    {#if editing}
      <!-- A plain textarea rather than `TextArea`: this is a full-height
           editing surface for the message being approved, not a form field in a
           labelled row, and it sizes against the card. -->
      <textarea
        class="draft-edit"
        bind:value={editBody}
        rows="10"
        aria-label="Message body"
        disabled={busy}
      ></textarea>
      <div class="draft-actions">
        <Button variant="primary" size="sm" disabled={busy} onclick={saveEdit}>Save</Button>
        <Button variant="subtle" size="sm" disabled={busy} onclick={() => (editing = false)}
          >Cancel</Button
        >
      </div>
    {:else}
      <div class="draft-body">{shownBody}</div>
      {#if longBody}
        <button class="draft-more" type="button" onclick={() => (expanded = !expanded)}>
          {expanded ? 'Show less' : 'Show the whole message'}
        </button>
      {/if}

      {#if actions.length > 0}
        <!-- Calendar writes are deliberately not gated, so a task can create an
             event and then have its email held. Declining without saying so
             leaves an orphan the user never hears about. Read-only: reversing
             would need a per-task ledger of reversible operations, which is its
             own feature (see the spec's Stage 5 carry-out). -->
        <div class="draft-also">
          <span class="micro-label">This task also</span>
          <ul>
            {#each actions as action, i (i)}
              <li>{action}</li>
            {/each}
          </ul>
        </div>
      {/if}

      <div class="draft-actions">
        <Button
          variant="primary"
          size="sm"
          disabled={busy}
          onclick={() => run(() => onApprove(draft.id))}>Send</Button
        >
        <Button variant="secondary" size="sm" disabled={busy} onclick={startEdit}>Edit</Button>
        <Button
          variant="subtle"
          size="sm"
          disabled={busy}
          onclick={() => run(() => onDiscard(draft.id))}>Discard</Button
        >
      </div>
    {/if}
  {/if}
</article>

<style>
  .draft-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    /* The same left-border cue `PendingConfirmations` uses for "this is waiting
       on you", so the two read as one class of thing across the surface. */
    border-left: 3px solid var(--status-warn-fg);
    border-radius: var(--radius-card);
    /* Uniform, and the same figure `.external` and `.cmd-output` use in
       `Message.svelte`: the three are the filled blocks a turn's content column
       holds, and they have to start their text at one inset. */
    padding: var(--space-2);
    /* No `max-width` here, deliberately. The `--chat-body-max` cap belongs to
       the turn placement, where the card is one block in a message's content
       column and has to match the body above it — so it is written there
       (`Message.svelte`), beside the spacing rule that is scoped to that slot
       for the same reason. Carried here it also applied in the banner, which is
       pane chrome sitting beside `PendingConfirmations`: the draft stopped
       134px short of the confirmation card directly above it, at the one width
       the two most obviously want to agree on. */
  }
  /* A stuck or unreadable row is a fault to look into rather than a decision to
     take, and the colour is the only thing saying so before the text is read. */
  .draft-card.attention {
    border-left-color: var(--status-danger-fg);
  }
  .draft-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .draft-head :global(.micro-label) {
    display: inline-flex;
    align-items: center;
    gap: 0.3em;
  }
  .draft-line {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }
  .draft-note {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
  /* The compact banner's two content lines. Both are single-line and clipped in
     CSS rather than cut to a character count: the card's width varies with the
     pane, and a count that fits at one width overflows or under-fills at
     another. `.draft-summary` carries the recipients and subject the fields
     grid would otherwise spend two rows on; `.draft-peek` stands in for the
     body, with its whitespace collapsed — a blank line in the source is exactly
     what made a short preview occupy three rows under `pre-wrap`. */
  .draft-summary,
  .draft-peek {
    margin: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .draft-summary {
    font-size: var(--text-sm);
    color: var(--text-primary);
  }
  .draft-peek {
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .draft-fields {
    margin: 0;
    --kv-gap: var(--space-2);
    font-size: var(--text-sm);
  }
  .draft-fields dd {
    overflow-wrap: anywhere;
  }
  .draft-body {
    /* `white-space: pre-wrap` and a text node, never markdown: what is approved
       has to be what is sent, byte for byte, and rendering a stranger's thread
       as markup is the one thing this feature exists to avoid. */
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: var(--text-sm);
    color: var(--text-reading);
    background: var(--surface-raised);
    border-radius: var(--radius-sm);
    padding: var(--space-2);
  }
  .draft-edit {
    width: 100%;
    font: inherit;
    line-height: 1.5;
    font-size: var(--text-sm);
    color: var(--text-primary);
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    padding: var(--space-2);
    resize: vertical;
  }
  /* `center` rather than the base `flex-start`, so that in the compact card's
     actions row it sits on the buttons' centre line instead of riding up
     against their top edge. Harmless where the row is a column. */
  .draft-actions .draft-more {
    align-self: center;
  }
  /* Pushed to the far end of the head row, away from the reason caption. */
  .draft-head .draft-more {
    margin-left: auto;
  }
  .draft-more {
    align-self: flex-start;
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    font-size: var(--text-xs);
    color: var(--link);
    cursor: pointer;
  }
  .draft-more:hover {
    text-decoration: underline;
  }
  .draft-also ul {
    margin: var(--space-1) 0 0;
    padding-left: var(--space-3);
    font-size: var(--text-xs);
    color: var(--text-secondary);
  }
  .draft-actions {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
</style>
