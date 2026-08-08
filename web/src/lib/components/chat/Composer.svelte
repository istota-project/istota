<script module lang="ts">
  /**
   * How long the field is left alone before its text is written down.
   *
   * Long enough that ordinary typing writes once a phrase rather than once a
   * letter, short enough that a pause and a tab away lands inside it. Every
   * departure the component can see — a key change, an unmount, the page going
   * away — flushes immediately, so this only ever bounds how much a *crash*
   * could cost.
   */
  export const DRAFT_SAVE_DEBOUNCE_MS = 400;
</script>

<script lang="ts">
  import { onDestroy, tick, untrack } from 'svelte';
  import {
    ArrowUp,
    Square,
    Plus,
    Mic,
    Check,
    Trash2,
    X,
    Image,
    Camera,
    Folder,
    Reply,
  } from 'lucide-svelte';
  import { IconButton } from '$lib/components/ui';
  import { uploadChatAttachment, chatConfigOnce, type ChatAttachment } from '$lib/api';
  import AutocompletePopover from './autocomplete/AutocompletePopover.svelte';
  import { createAutocomplete, type AcceptResult } from './autocomplete/useAutocomplete.svelte';
  import { commandProvider, modelAliasProvider } from './autocomplete/providers';
  import { createRecorder, formatElapsed } from './useRecorder.svelte';
  import { usesSoftKeyboard } from '$lib/platform/input';
  import {
    nativePickersAvailable,
    takePhoto,
    pickPhotos,
    pickDocuments,
    pickedFromFile,
    type Picked,
  } from '$lib/platform/nativePicker';
  import { readDraft, readDraftReply, writeDraft } from '$lib/stores/drafts';
  import type { MessageReply } from '$lib/stores/segments';

  let {
    onSend,
    onCancel,
    busy = false,
    placeholder = 'Message Istota…',
    draftKey = null,
    sendSettled = { n: 0, key: null },
    replyTo = null,
    onReplyChange,
    restoreSend = null,
  }: {
    onSend: (text: string, attachments: ChatAttachment[], replyTo?: MessageReply | null) => void;
    onCancel?: () => void;
    busy?: boolean;
    placeholder?: string;
    /**
     * Where unsent text is held between visits, or null to hold none. The
     * caller owns the namespace — the chat page passes
     * `` `${userId}:room:${token}` ``, which is what makes a draft per-room
     * and per-person; the module header explains why both halves are in it.
     * Omitting the prop leaves the composer exactly as it was: whatever is
     * typed lives and dies with the component.
     */
    draftKey?: string | null;
    /**
     * The last send the backend acked: a counter that only ever increases,
     * and the draft key it belongs to.
     *
     * The draft is dropped on the ack rather than on submit, because until
     * then the stored draft is the only copy of that text which survives a
     * reload — the failed row does not. The signal comes in as data rather
     * than a callback because the composer is the one that acts on it: handing
     * it a function to call itself would be circular.
     *
     * The key travels with the counter because two sends can be open at once
     * — switching rooms un-gates the composer — and a bare counter would let
     * whichever acked first drop the other's draft while its own send was
     * still in flight, destroying the only copy of it.
     */
    sendSettled?: { n: number; key: string | null };
    /**
     * The message the next send will cite, or null.
     *
     * Held by the caller rather than here, because a citation is resolved
     * against the transcript — the composer knows only the id, and the author
     * label and excerpt come from the row. The composer reports the id it
     * wants staged and reads back what the caller resolved.
     */
    replyTo?: MessageReply | null;
    /**
     * Stage a different citation, or clear it. Called with the id alone: the
     * caller owns the resolution, so a restored draft (which stores only an
     * id) and a tapped Reply come back through the same path.
     */
    onReplyChange?: (msgId: number | null) => void;
    /**
     * A send handed back to be edited and re-sent, with a counter so the same
     * text can come back twice.
     *
     * The one case that refills the field — a citation the server refused,
     * whose message never became a durable row and whose Retry could not work.
     * Data rather than a callback, for the same reason `sendSettled` is: the
     * composer is what acts on it.
     */
    restoreSend?: { n: number; text: string; attachments: ChatAttachment[] } | null;
  } = $props();

  let text = $state('');
  let textarea: HTMLTextAreaElement | undefined = $state();
  // The one file input left, and only the off-shell path reaches it: unfiltered
  // and multiple, exactly as before the attachment menu existed. The menu's own
  // rows go through the native pickers instead.
  let fileInput: HTMLInputElement | undefined = $state();
  // Measured to derive the single-row field width — see wrapsAtSingleRowWidth.
  let rowEl: HTMLDivElement | undefined = $state();
  let plusEl: HTMLButtonElement | undefined = $state();
  let toolsEl: HTMLDivElement | undefined = $state();
  let wrapEl: HTMLDivElement | undefined = $state();
  let attachments = $state<ChatAttachment[]>([]);
  let uploading = $state(0);
  let dragOver = $state(false);
  let uploadError = $state('');
  // Bumped whenever the composer changes room. `upload` is async and appends
  // to `attachments` with no notion of where it started, while `switchDraft`
  // clears them synchronously and cannot see a promise in flight — so a large
  // file picked in one room and resolved after a switch put its chip in the
  // new room, and sending from there posted it to the wrong one.
  let uploadEpoch = 0;
  // What the server will accept, or null until /chat/config answers. See
  // refusalReason for why null means "ask the server" rather than a default.
  let limits = $state<{ maxBytes: number; maxMb: number; extensions: string[] } | null>(null);
  chatConfigOnce()
    .then((cfg) => {
      limits = {
        maxBytes: cfg.max_attachment_mb * 1024 * 1024,
        maxMb: cfg.max_attachment_mb,
        extensions: cfg.attachment_extensions ?? [],
      };
    })
    .catch(() => {});
  // True once the text no longer fits on one line: the controls drop below the
  // field instead of sharing its row. Driven by the measurement in autoGrow,
  // not a media query, so it tracks content rather than viewport width.
  let multiline = $state(false);

  // ── Drafts ────────────────────────────────────────────────────────────────
  //
  // Text that has been typed but not sent survives leaving the room and
  // leaving the page. It is held under `draftKey`, so switching rooms swaps
  // the field's contents rather than carrying one room's half-written message
  // into another — which is what the composer did before, since it is mounted
  // once and never keyed on the room.
  //
  // `activeDraftKey` shadows the prop rather than being read from it: the
  // outgoing key is what the field's current text has to be written under, and
  // by the time an effect sees a change the prop is already the incoming one.
  let activeDraftKey: string | null = null;
  // Whether a real key has ever been seen. Distinguishes "the room list has
  // not answered yet" from "the room went away under us", which look identical
  // from the prop alone and want opposite treatment — see switchDraft.
  let hasHadDraftKey = false;
  let saveTimer: ReturnType<typeof setTimeout> | undefined;
  // Messages handed to `onSend` whose backend ack has not arrived, by draft
  // key. Until an ack lands, that key's emptied field is not a cleared draft —
  // the text went to the server and the stored copy is what a reload would
  // restore if it never got there.
  //
  // A map rather than one slot because two sends can be open at once: leaving
  // a room resets the session's status to idle, which un-gates the composer,
  // so a second room's send can start while the first's POST is still open.
  // With a single slot the second submit overwrote the first, and the first
  // ack then dropped the second room's draft while its send was still in
  // flight — destroying the only copy of it.
  //
  // Entries live for the component's lifetime, so a send that never lands
  // holds its key until reload. That is the point: the failed row carries the
  // message with a Retry for as long as the session lasts, and after a reload
  // there is no map and `readDraft` restores the text.
  const unsettledSends = new Map<string, string>();

  /** Write the field down now, cancelling any pending debounced write. */
  function flushDraft() {
    clearTimeout(saveTimer);
    saveTimer = undefined;
    if (!activeDraftKey) return;
    // Hold the submitted text through the ack. Every departure flushes — a
    // room switch, an unmount, `pagehide` — so without this the draft the
    // whole mechanism exists to keep would be dropped by the reload itself.
    if (unsettledSends.has(activeDraftKey) && !text.trim()) return;
    // The staged citation is part of the unsent message, so it goes down with
    // it. An empty body drops the entry outright, which is right: a citation
    // is not itself a message.
    writeDraft(activeDraftKey, text, replyTo?.msgId);
  }

  /**
   * The backend has the message under `key`: it is no longer a draft.
   *
   * Withheld when something has been typed since, which is a live draft again
   * and nothing to do with the message that just landed.
   */
  function settleDraft(key: string | null) {
    if (!key) return;
    const sent = unsettledSends.get(key);
    if (sent === undefined) return;
    unsettledSends.delete(key);
    // The field is the live copy only while its own room is on screen; an ack
    // landing after a switch has to judge by what is stored instead. Emptiness
    // is a sufficient test for the on-screen case only because `switchDraft`
    // refuses to restore a message that is still in flight — otherwise coming
    // back to the room would look exactly like having typed it again.
    const typedSince = key === activeDraftKey ? !!text.trim() : readDraft(key) !== sent;
    if (!typedSince) writeDraft(key, '', undefined);
  }

  // The count at mount, deliberately: only a *change* is an ack, and a
  // remounted composer must not settle a draft on the count it inherits.
  let seenSettle = untrack(() => sendSettled.n);
  $effect(() => {
    const { n, key } = sendSettled;
    untrack(() => {
      if (n === seenSettle) return;
      seenSettle = n;
      settleDraft(key);
    });
  });

  // Same mount-time seed as `seenSettle`: only a change is a hand-back, and a
  // remounted composer must not refill itself from the count it inherits.
  let seenRestore = untrack(() => restoreSend?.n ?? 0);
  $effect(() => {
    const r = restoreSend;
    untrack(() => {
      if (!r || r.n === seenRestore) return;
      seenRestore = r.n;
      // The message never became a durable row, so this is the only copy left.
      // The draft entry the submit wrote is cleared as a side effect of the
      // flush below, now that the field holds the text again.
      setText(r.text);
      attachments = r.attachments;
      if (activeDraftKey) unsettledSends.delete(activeDraftKey);
      flushDraft();
      textarea?.focus();
    });
  });

  function scheduleDraftSave() {
    if (!activeDraftKey) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushDraft, DRAFT_SAVE_DEBOUNCE_MS);
  }

  function setText(next: string) {
    if (next === text) return;
    text = next;
    queueMicrotask(autoGrow);
  }

  function switchDraft(next: string | null) {
    if (next === activeDraftKey) return;
    // A pending completion is spliced into the text the engine last saw, so
    // leaving the popover open across a swap would write the outgoing room's
    // text back into the incoming one's field.
    ac.close();
    // Text typed while no key has *ever* been set belongs to whichever room
    // lands: the composer is on screen before the room list answers, so the
    // opening keystrokes of a page load have no room to be attributed to yet.
    //
    // A key going null *later* means something else entirely — the room was
    // deleted, archived, or removed by another client, all of which leave the
    // view on 'room' with the composer still mounted. Treating that as the
    // same case is the leak this whole mechanism exists to stop: the departed
    // room's text would be handed to whichever room is picked next.
    const carried = hasHadDraftKey ? '' : text;
    const leavingRoom = activeDraftKey !== null;
    flushDraft();
    activeDraftKey = next;
    if (next) hasHadDraftKey = true;
    // Attachments are not drafted — they are already uploaded, so the chips
    // are a view of server-side files rather than the record of them — but
    // they must not ride along either. Re-picking a file costs a tap; posting
    // one to the wrong room does not undo.
    //
    // An upload still in flight cannot be reached by clearing the list, so the
    // epoch is bumped alongside it: `upload` compares on the way out and drops
    // a result whose room has since changed. The counter and the error are
    // reset with it, both being the state of an upload that is no longer this
    // room's business.
    if (leavingRoom) {
      uploadEpoch++;
      attachments = [];
      uploading = 0;
      uploadError = '';
    }
    if (!next) {
      if (leavingRoom) {
        setText('');
        // A citation names a message in the room being left, so it cannot
        // survive the departure the way carried text can.
        onReplyChange?.(null);
        // The field has been cleared of the departed room's text, so what is
        // typed from here belongs to whichever room lands next — the same case
        // as the opening keystrokes of a page load. Without the reset the
        // one-shot flag would suppress that carry forever.
        hasHadDraftKey = false;
      }
      return;
    }
    // A stored draft outranks carried text. The carry exists for a field that
    // was empty when the page loaded; letting one keystroke typed during that
    // window replace the draft the user came back for would destroy it, with
    // no undo and no notice.
    //
    // A message still waiting on its ack is not a draft to restore, though it
    // is stored under this key: `submit` writes it there so a reload can
    // recover it if the send never lands. Putting it back in the field would
    // show a message the user has already sent as unsent text, one Enter away
    // from being sent twice — and the ack would then read it as newly typed
    // and decline to clear it, so it would stay there.
    const stored = readDraft(next);
    const restorable = unsettledSends.get(next) === stored ? '' : stored;
    setText(restorable || carried);
    // The citation follows the text it belongs to: restored with a restored
    // draft, cleared otherwise. Carried text is text typed before any room
    // existed, so it can carry no citation.
    onReplyChange?.(restorable ? (readDraftReply(next) ?? null) : null);
    // Carried text has never been written anywhere — the field is its only
    // copy until this lands.
    if (carried && !restorable) flushDraft();
  }

  $effect(() => {
    const next = draftKey ?? null;
    untrack(() => switchDraft(next));
  });

  onDestroy(flushDraft);

  const recorder = createRecorder({ onComplete: (file) => upload([file]) });
  onDestroy(() => recorder.dispose());

  const AUDIO_EXT = new Set(['webm', 'm4a', 'mp3', 'ogg', 'wav', 'mp4', 'opus', 'aac']);
  const isAudio = (name: string) => AUDIO_EXT.has(name.split('.').pop()?.toLowerCase() ?? '');

  // Prefix autocomplete. modelAliasProvider is ordered first so `!model <alias>`
  // (with the space) wins over the bare-! command matcher.
  const AC_LIST_ID = 'chat-ac-listbox';
  const acOptionId = (key: string) => `chat-ac-opt-${key}`;
  const ac = createAutocomplete([modelAliasProvider(), commandProvider()], {
    onAccept: applyAccept,
  });
  let acActiveDescendant = $derived(
    ac.open && ac.suggestions[ac.activeIndex]
      ? acOptionId(ac.suggestions[ac.activeIndex].key)
      : undefined,
  );

  function syncAc() {
    if (textarea) ac.sync(textarea.value, textarea.selectionStart ?? textarea.value.length);
  }

  async function applyAccept(r: AcceptResult) {
    text = r.text;
    await tick();
    if (textarea) {
      textarea.setSelectionRange(r.caret, r.caret);
      autoGrow();
      // Re-sync so a chained trigger (e.g. `!model ` → alias list) activates.
      ac.sync(text, r.caret);
    }
  }

  // Grow the field to its content, capped at MAX_LINES. Everything is measured
  // off the computed line-height rather than a px constant so the cap stays the
  // same *number of lines* at every text-size setting.
  const MAX_LINES = 8;

  function lineMetrics(): { line: number; pad: number } {
    const cs = getComputedStyle(textarea!);
    return {
      line: parseFloat(cs.lineHeight) || 20,
      pad: (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0),
    };
  }

  // Width the field has while it shares its row with the controls. Derived from
  // the row rather than read off the field, because the field is *wider* than
  // this whenever it has already wrapped.
  function singleRowWidth(): number {
    if (!rowEl || !plusEl || !toolsEl) return 0;
    const cs = getComputedStyle(rowEl);
    const gap = parseFloat(cs.columnGap) || 0;
    const inner =
      rowEl.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
    const w = inner - plusEl.offsetWidth - toolsEl.offsetWidth - gap * 2;
    return w > 0 ? w : 0;
  }

  // Whether the text wraps — always measured at the single-row width, never at
  // the field's current width. Measuring the current width is a feedback loop:
  // wrapping moves the controls out of the field's row, which widens the field,
  // which can un-wrap the text, which flips it back. The field then alternated
  // between one and two rows on consecutive keystrokes.
  function wrapsAtSingleRowWidth(line: number, pad: number): boolean | null {
    const narrow = singleRowWidth();
    const prevHeight = textarea!.style.height;
    // Constrain the wrapper, not the field: the field is a flex item with
    // `flex: 1` (basis 0), so its `width` is ignored for sizing and setting it
    // would leave the measurement at whatever width the field already had —
    // which is the feedback loop this function exists to break. The inline
    // flex-basis also beats the `.multiline .ta-wrap` rule.
    const prevBasis = wrapEl?.style.flexBasis ?? '';
    const prevGrow = wrapEl?.style.flexGrow ?? '';
    if (narrow > 0 && wrapEl) {
      wrapEl.style.flexBasis = `${narrow}px`;
      wrapEl.style.flexGrow = '0';
    }
    // The single-row *padding* too, not just the width: multiline widens the
    // field's horizontal padding, which narrows the content box and would make
    // the text still look wrapped for a couple of characters after it stopped
    // being — the field would sit on two rows longer than it should on the way
    // back down. Referenced by name so the values stay defined once, in the CSS.
    const prevPadX = [textarea!.style.paddingLeft, textarea!.style.paddingRight];
    textarea!.style.paddingLeft = 'var(--ta-pad-x-single)';
    textarea!.style.paddingRight = 'var(--ta-pad-x-single)';
    textarea!.style.height = 'auto';
    const content = textarea!.scrollHeight;
    if (wrapEl) {
      wrapEl.style.flexBasis = prevBasis;
      wrapEl.style.flexGrow = prevGrow;
    }
    [textarea!.style.paddingLeft, textarea!.style.paddingRight] = prevPadX;
    textarea!.style.height = prevHeight;
    // jsdom reports scrollHeight 0 — no answer rather than a wrong one.
    return content > 0 ? content > line + pad + 2 : null;
  }

  function applyHeight() {
    if (!textarea) return;
    const { line, pad } = lineMetrics();
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, line * MAX_LINES + pad) + 'px';
  }

  async function autoGrow() {
    if (!textarea) return;
    const { line, pad } = lineMetrics();
    const wraps = wrapsAtSingleRowWidth(line, pad);
    if (wraps !== null) multiline = wraps;
    // Size the field only once the class change has landed, so the height is
    // measured against the width the field actually ends up with.
    await tick();
    applyHeight();
  }

  // iOS Safari auto-zooms when a focused input renders below 16px. Rather than
  // inflating the field's font (which throws off its height vs. the buttons),
  // pin the viewport's maximum-scale only while the textarea is focused, then
  // restore it on blur so pinch-to-zoom keeps working everywhere else.
  // Both strings must keep viewport-fit=cover (app.html sets it): dropping it on
  // focus would re-letterbox the page mid-interaction and jump the layout.
  const VIEWPORT_DEFAULT = 'width=device-width, initial-scale=1, viewport-fit=cover';
  const VIEWPORT_NO_ZOOM =
    'width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover';

  function setViewport(content: string) {
    const meta = document.querySelector('meta[name="viewport"]');
    if (meta) meta.setAttribute('content', content);
  }
  // Attaching a file costs the keyboard, and it should not.
  //
  // The tap on the button is handled — it cancels its own focus shift, see
  // keepFocus — but activating a file input hands the rest to WebKit, which
  // raises its own action sheet and takes first responder off the field. The
  // page has no say in that, and no way back from it either: a programmatic
  // `.focus()` does not raise the keyboard in a WKWebView at all
  // (ionic-team/capacitor#334), so there is nothing to restore it with.
  //
  // So the first tap does not reach a file input. It opens this menu, which is
  // part of the page and which the keyboard has no reason to move for, and the
  // rows go straight to the source they name — through the shell's native
  // pickers, because a file input cannot be aimed at one. `capture` is the only
  // hint HTML carries and `accept` is unreliable on iOS (rdar://36726477), so
  // routing the rows back through file inputs would only put our menu in front
  // of WebKit's, which is a step added rather than removed.
  //
  // Off-shell there is no menu at all: without native pickers every row would
  // end at the same sheet, so the button opens the file input directly and the
  // browser behaves exactly as it did before any of this.
  //
  // Focus deliberately stays in the textarea while the menu is open (the rows
  // cancel their own focus shift too, and the menu renders inside `.composer`,
  // which installKeyboardDismiss exempts). That is the same bargain the
  // autocomplete popover makes: a menu that took focus would drop the keyboard
  // itself, which is the entire thing being avoided.
  let attachMenuOpen = $state(false);

  function onAttachClick() {
    if (nativePickersAvailable()) attachMenuOpen = !attachMenuOpen;
    else fileInput?.click();
  }

  function onWindowPointerDown(e: PointerEvent) {
    if (!attachMenuOpen) return;
    const target = e.target as Element | null;
    // The attach button's own press arrives here before its click. Closing on
    // it would leave the click to reopen the menu, so it is left to the toggle.
    if (target?.closest?.('.attach-menu, .plus')) return;
    attachMenuOpen = false;
  }

  function onWindowKeydown(e: KeyboardEvent) {
    if (attachMenuOpen && e.key === 'Escape') attachMenuOpen = false;
  }

  /** Run one of the native pickers and attach whatever it hands back. */
  async function pickWith(source: () => Promise<Picked[]>) {
    attachMenuOpen = false;
    uploadError = '';
    try {
      const files = await source();
      if (files.length) upload(files);
    } catch {
      // The picker itself failed — a cancel resolves empty rather than throwing.
      uploadError = 'Could not open the picker.';
    }
  }

  function onFilesChosen(e: Event) {
    const input = e.target as HTMLInputElement;
    if (input.files) upload(input.files);
    input.value = '';
  }

  function onFocus() {
    setViewport(VIEWPORT_NO_ZOOM);
  }
  function onBlur() {
    setViewport(VIEWPORT_DEFAULT);
    // iOS scrolls the *window* to bring a focused field above the keyboard and
    // does not reliably undo it on dismissal. The app is exactly one viewport
    // tall with its own internal scrollers, so a non-zero window scroll has
    // nowhere to go and leaves a dead band where the layout ran off the bottom —
    // the "stuck at less than full height" state, most visible in an iOS
    // home-screen web app where there is no browser chrome to hide it. Nothing
    // legitimately scrolls the window here, so resetting it is unconditional.
    if (window.scrollY !== 0) window.scrollTo(0, 0);
    // The popover accepts on mousedown (preventDefault keeps focus), so a
    // click on a row does not blur first — safe to close here.
    ac.close();
  }

  /**
   * Why the server would turn this file away, or null if it would take it.
   *
   * Both numbers are the server's, fetched once from /chat/config, so there is
   * one place to change the ceiling and the client follows. Worth checking here
   * because the 413 only arrives after the whole body has been read: on a phone
   * that is a long wait to be told no.
   *
   * `limits` stays null until the config lands, and a config that never lands
   * leaves it null for good. That is deliberate — an unreachable /chat/config
   * must not become a client-side refusal of files the server would have taken.
   */
  function refusalReason(file: Picked): string | null {
    if (!limits) return null;
    if (file.size > limits.maxBytes) {
      return `${file.name} is larger than ${limits.maxMb} MB.`;
    }
    const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
    if (limits.extensions.length && !limits.extensions.includes(ext)) {
      return `.${ext} is not a file type this server accepts.`;
    }
    return null;
  }

  /**
   * The one sink every attachment goes through.
   *
   * A pick from the native menu arrives as a path the shell will post itself; a
   * paste, drop, file input or voice memo arrives as bytes already in the page.
   * Both become a `Picked` here so the size check, the error handling and the
   * chip list have one shape to deal with.
   */
  async function upload(files: FileList | File[] | Picked[]) {
    // The room this batch belongs to. Every write below is guarded on it still
    // being the current one — including the counter, which `switchDraft` has
    // already reset to 0, so a stale decrement would drive it negative and
    // leave the composer permanently reporting an upload in progress.
    const epoch = uploadEpoch;
    const stale = () => epoch !== uploadEpoch;
    uploadError = '';
    const items = Array.from(files).map((f) => (f instanceof File ? pickedFromFile(f) : f));
    for (const file of items) {
      if (stale()) return;
      const refusal = refusalReason(file);
      if (refusal) {
        // Keep going: one file being too big is no reason to drop the others.
        uploadError = refusal;
        continue;
      }
      uploading++;
      try {
        const att = await uploadChatAttachment(file);
        // The file did reach the server and is now orphaned there — the same
        // outcome as closing the tab mid-upload, which the inbox already
        // tolerates. Better than a chip in a room the file was not picked in.
        if (!stale()) attachments = [...attachments, att];
      } catch (e) {
        if (!stale()) uploadError = e instanceof Error ? e.message : 'upload failed';
      } finally {
        if (!stale()) uploading--;
      }
    }
  }

  function removeAttachment(path: string) {
    attachments = attachments.filter((a) => a.path !== path);
  }

  function submit() {
    const t = text.trim();
    // A citation does not make an empty message sendable — it is a pointer,
    // not content.
    if (!t && attachments.length === 0) return;
    onSend(t, attachments, replyTo);
    text = '';
    attachments = [];
    // The citation belongs to the message that just left. Cleared here rather
    // than on the ack, unlike the draft: a `!command` returns from inside the
    // request with no task to attach one to, so letting it persist would carry
    // it silently into the next message.
    onReplyChange?.(null);
    // The draft is *not* dropped here. Until the backend acks, the stored
    // draft is the only copy of this text that survives a reload — the failed
    // row does not — so the drop waits for `settleDraft`.
    //
    // Written rather than merely left alone: a submit inside the debounce
    // window would otherwise leave nothing stored at all, or a stale prefix of
    // what was sent. Cancelling the timer first stops that write landing after
    // this one and clearing it back out.
    if (activeDraftKey) {
      clearTimeout(saveTimer);
      saveTimer = undefined;
      // What was stored, not what was passed: an over-long message is held
      // clamped, and both the tests that recognise it again — the ack in
      // `settleDraft` and the restore refusal in `switchDraft` — compare
      // against the stored copy.
      unsettledSends.set(activeDraftKey, writeDraft(activeDraftKey, t));
    }
    queueMicrotask(autoGrow);
    // A sent message is the end of a turn, and the reply arrives in the third of
    // the screen the keyboard is standing on. Where the keyboard is soft, giving
    // that space back is what the user was going to do next anyway. Where it is
    // hardware, focus is free and they are probably still typing, so it stays.
    if (usesSoftKeyboard()) textarea?.blur();
  }

  // The send/stop control is one button in two modes — see the markup below.
  const canSend = $derived(!!text.trim() || attachments.length > 0);
  const showStop = $derived(busy && !!onCancel);

  // Belt to the single-element brace: even on one element, a tap can be
  // delivered twice (a compat mouse event after the touch), and the second
  // delivery lands after the mode has flipped — so it would read as the
  // *opposite* command. Drop an activation whose mode differs from the one the
  // last activation ran in, within the window a duplicate could arrive in.
  // A repeat in the same mode is left alone: two genuine sends are harmless,
  // and a real stop is never this fast (the mode only just changed under it).
  const MODE_FLIP_GUARD_MS = 400;
  let lastActivationAt = 0;
  let lastActivationMode: 'send' | 'stop' | null = null;

  function activatePrimary() {
    const mode: 'send' | 'stop' = showStop ? 'stop' : 'send';
    const now = Date.now();
    if (
      lastActivationMode !== null &&
      mode !== lastActivationMode &&
      now - lastActivationAt < MODE_FLIP_GUARD_MS
    ) {
      return;
    }
    lastActivationAt = now;
    lastActivationMode = mode;
    if (mode === 'stop') onCancel?.();
    else submit();
  }

  // Tapping a button with the soft keyboard up used to cost two taps, of which
  // the first only closed the keyboard. iOS takes focus off the field when a
  // button takes the tap, and the keyboard leaving reflows the composer down
  // through the space it was standing in — all before the synthesized click is
  // hit-tested, which then lands where the button no longer is.
  //
  // Suppressing the default focus shift keeps the field focused through the
  // click, so the button gets its activation and `submit()` drops the keyboard
  // itself, once the message has gone.
  //
  // On mousedown rather than pointerdown: preventing the compatibility mouse
  // event is what suppresses the focus change, and it leaves the click intact,
  // which cancelling the pointer event is not guaranteed to do. The textarea is
  // deliberately not covered — its own mousedown default is what places the
  // caret.
  function keepFocus(e: MouseEvent) {
    e.preventDefault();
  }

  function onInput() {
    autoGrow();
    syncAc();
    scheduleDraftSave();
  }

  // Caret-only moves (no text change) don't fire input; re-evaluate the match
  // on the arrow/home/end keys and on click so the trigger tracks the caret.
  const CARET_KEYS = new Set(['ArrowLeft', 'ArrowRight', 'Home', 'End']);
  function onKeyup(e: KeyboardEvent) {
    if (CARET_KEYS.has(e.key)) syncAc();
  }

  function onKeydown(e: KeyboardEvent) {
    // Return writes a newline; sending is Cmd/Ctrl+Enter or the button. Enter
    // used to submit, which made a paragraph break impossible to type — every
    // one of them sent the message instead. Shift+Enter is left alone rather
    // than repurposed as the send chord: it means newline everywhere else, and
    // this field is not the place to teach someone otherwise.
    //
    // Ahead of the autocomplete, which takes a bare Enter to accept a
    // completion. That is the right owner of the unmodified key, but the chord
    // is unambiguous — with the popover open it still means send, not "accept
    // the row and stay put".
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      // Only when the button would have sent. The chord was the one path into
      // a send that did not consult the mode, so holding it started a second
      // turn in a room that already had one — and two overlapping turns share
      // one echo buffer, whose drain then releases the first turn's frames
      // before its task id exists. Silently doing nothing rather than being
      // repurposed as Stop: a chord that cancels work is not what it looks
      // like it does.
      if (!showStop) submit();
      return;
    }
    // The engine consumes Arrow/Tab/Enter/Escape only while the popover is
    // open; when closed it returns false and the key does what it says.
    if (ac.onKeydown(e)) {
      e.preventDefault();
      return;
    }
    // Escape reaches the chip only once every open menu has declined it. The
    // order is the whole rule: a chip that took Escape first would dismiss the
    // citation while the user was looking at the menu they meant to close.
    // Both menus count — the attach menu deliberately leaves focus in the
    // textarea, so its Escape bubbles through here on the way to the window
    // handler that closes it.
    if (e.key === 'Escape' && replyTo && !attachMenuOpen) {
      e.preventDefault();
      onReplyChange?.(null);
    }
  }

  function onPaste(e: ClipboardEvent) {
    const files = Array.from(e.clipboardData?.files ?? []);
    if (files.length) {
      e.preventDefault();
      upload(files);
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragOver = false;
    if (e.dataTransfer?.files?.length) upload(e.dataTransfer.files);
  }
</script>

<!-- The wrap point moves with the field's width, so a rotation or a sidebar
     toggle has to re-evaluate it; nothing else fires while the text is idle.

     Two flushes beyond the destroy one, which only covers navigating within
     the app. `pagehide` is a reload, a closed tab or a navigation away — the
     event a WKWebView delivers where `unload` is not. It does *not* cover the
     app being sent to the background, which fires `visibilitychange` → hidden
     and may never fire anything again if iOS then discards the page; that is
     the last callback there is, and the reason the store's own poller already
     hangs off it. Without the second one the loss is bounded by 400ms of
     *idle*, not of typing — continuous typing never reaches a debounce. -->
<svelte:window
  onresize={autoGrow}
  onpointerdown={onWindowPointerDown}
  onkeydown={onWindowKeydown}
  onpagehide={flushDraft}
/>
<svelte:document
  onvisibilitychange={() => {
    if (document.visibilityState === 'hidden') flushDraft();
  }}
/>

<div
  class="composer"
  class:drag={dragOver}
  role="group"
  aria-label="Message composer"
  ondragover={(e) => {
    e.preventDefault();
    dragOver = true;
  }}
  ondragleave={() => (dragOver = false)}
  ondrop={onDrop}
>
  <!-- Inside `.composer` on purpose: that is the selector installKeyboardDismiss
       exempts, so tapping a row is not also a tap "outside the composer". -->
  {#if attachMenuOpen}
    <div class="attach-menu" role="menu" aria-label="Attach">
      <button
        class="attach-menu-item"
        type="button"
        role="menuitem"
        aria-label="Photo Library"
        onmousedown={keepFocus}
        onclick={() => pickWith(pickPhotos)}
      >
        <Image size={16} />
        Photo Library
      </button>
      <button
        class="attach-menu-item"
        type="button"
        role="menuitem"
        aria-label="Take Photo"
        onmousedown={keepFocus}
        onclick={() => pickWith(takePhoto)}
      >
        <Camera size={16} />
        Take Photo
      </button>
      <button
        class="attach-menu-item"
        type="button"
        role="menuitem"
        aria-label="Choose File"
        onmousedown={keepFocus}
        onclick={() => pickWith(pickDocuments)}
      >
        <Folder size={16} />
        Choose File
      </button>
    </div>
  {/if}
  <!-- The staged citation, above the field, so what the next send answers is
       visible while it is being written. -->
  {#if replyTo}
    <div class="reply-chip">
      <Reply size={13} class="reply-chip-icon" />
      <span class="reply-chip-text">{replyTo.excerpt ?? 'the selected message'}</span>
      <IconButton
        size="sm"
        label="Clear reply"
        onclick={() => onReplyChange?.(null)}
        title="Clear reply"
      >
        <X size={13} />
      </IconButton>
    </div>
  {/if}
  {#if attachments.length || uploading}
    <div class="attach-row">
      {#each attachments as att (att.path)}
        <span class="attach-chip">
          {isAudio(att.name) ? '🎤' : '📎'}
          {att.name}
          <button
            class="attach-x"
            onclick={() => removeAttachment(att.path)}
            type="button"
            aria-label="Remove {att.name}"
          >
            <X size={11} />
          </button>
        </span>
      {/each}
      {#if uploading}<span class="attach-chip uploading">Uploading…</span>{/if}
    </div>
  {/if}
  {#if uploadError || recorder.error}
    <div class="notice-row">
      {#if uploadError}<span class="attach-error">{uploadError}</span>{/if}
      {#if recorder.error}<span class="attach-error">{recorder.error}</span>{/if}
    </div>
  {/if}

  <div class="composer-row" class:multiline bind:this={rowEl}>
    <button
      bind:this={plusEl}
      class="icon-btn plus"
      onmousedown={keepFocus}
      onclick={onAttachClick}
      type="button"
      aria-label="Attach file"
      title="Attach file"
      aria-haspopup="menu"
      aria-expanded={attachMenuOpen}
    >
      <Plus />
    </button>
    <!-- The off-shell fallback. Tapping it raises WebKit's own sheet, which is
         what a browser has always done here and the reason the menu above is
         gated on the native pickers being there to make it worth a tap. -->
    <input
      bind:this={fileInput}
      data-picker="file"
      type="file"
      multiple
      class="file-hidden"
      onchange={onFilesChosen}
    />
    <div class="ta-wrap" bind:this={wrapEl}>
      {#if ac.open}
        <AutocompletePopover
          suggestions={ac.suggestions}
          activeIndex={ac.activeIndex}
          listId={AC_LIST_ID}
          optionId={acOptionId}
          onaccept={(i) => ac.accept(i)}
          onhover={(i) => ac.setActive(i)}
        />
      {/if}
      <!-- `enterkeyhint` is `enter`, not `send`: the return key inserts a
           newline, and on a phone the send button is the send affordance —
           there is no modifier key to press with Enter there. Labelling it
           "send" would promise the opposite of what it does. -->
      <textarea
        bind:this={textarea}
        bind:value={text}
        oninput={onInput}
        onkeydown={onKeydown}
        onkeyup={onKeyup}
        onclick={syncAc}
        onpaste={onPaste}
        onfocus={onFocus}
        onblur={onBlur}
        {placeholder}
        rows="1"
        enterkeyhint="enter"
        aria-label="Message"
        role="combobox"
        aria-expanded={ac.open}
        aria-controls={AC_LIST_ID}
        aria-autocomplete="list"
        aria-activedescendant={acActiveDescendant}
      ></textarea>
      <!-- Overlaid rather than swapped in, so the textarea keeps its element
           identity (and any in-flight autocomplete state) across a recording. -->
      {#if recorder.recording || recorder.starting}
        <div class="rec-overlay" aria-live="polite">
          <span class="rec-dot" aria-hidden="true"></span>
          {#if recorder.starting}
            Starting…
          {:else}
            Recording {formatElapsed(recorder.elapsedMs)}
          {/if}
        </div>
      {/if}
    </div>
    <div class="tools" bind:this={toolsEl}>
      {#if recorder.recording}
        <button
          class="icon-btn"
          onmousedown={keepFocus}
          onclick={() => recorder.cancel()}
          type="button"
          aria-label="Discard recording"
          title="Discard recording"
        >
          <Trash2 />
        </button>
        <button
          class="icon-btn send"
          onmousedown={keepFocus}
          onclick={() => recorder.stop()}
          type="button"
          aria-label="Finish recording"
          title="Finish recording"
        >
          <Check />
        </button>
      {:else}
        {#if recorder.supported}
          <button
            class="icon-btn"
            onmousedown={keepFocus}
            onclick={() => recorder.start()}
            type="button"
            disabled={recorder.starting}
            aria-label="Record voice message"
            title="Record voice message"
          >
            <Mic />
          </button>
        {/if}
        <!-- One button across both modes, never an {#if} swap. Sending flips
             `busy` synchronously inside the click handler, so a swap would
             destroy the element the tap is still resolving against — and iOS
             Safari re-hit-tests when it delivers the synthesized click, landing
             it on whatever now occupies that spot. That is a tap on Send
             arriving as a tap on Stop: the task was cancelled the instant it
             started, and only a second client (rendering the room stream)
             showed it, because the sending client had already moved on. -->
        <button
          class="icon-btn send"
          class:stop={showStop}
          onmousedown={keepFocus}
          onclick={activatePrimary}
          type="button"
          disabled={showStop ? false : !canSend}
          aria-label={showStop ? 'Stop' : 'Send'}
          title={showStop ? 'Stop' : 'Send'}
        >
          {#if showStop}
            <Square />
          {:else}
            <ArrowUp />
          {/if}
        </button>
      {/if}
    </div>
  </div>
</div>

<style>
  /* No fill of its own: the composer floats over the transcript (the page docks
	   it absolutely at the bottom of the chat pane) and the page paints the fade
	   layer behind it, so a backdrop here would just cover that gradient. Every
	   child that needs to stay legible over passing text carries its own chip
	   fill — the pill below, the attachment chips, the error chips. */
  .composer {
    position: relative;
    padding: var(--space-2) var(--space-3);
    /* The composer is the bottom edge of the app, so it absorbs the device's
		   safe-area insets: the fill still runs into the corners / under the home
		   indicator, but the textarea and buttons sit above them. max() keeps the
		   normal padding as the floor, so this is inert wherever the insets are 0. */
    padding-bottom: max(0.6rem, var(--safe-bottom));
    padding-left: max(0.75rem, var(--safe-left));
    padding-right: max(0.75rem, var(--safe-right));
  }
  .composer.drag {
    background: var(--surface-raised);
    outline: 1px dashed var(--border-default);
    border-radius: 1.6em;
  }
  /* One pill owns the border, radius and focus ring; the field inside is
	   borderless. Every internal size is em-relative to this font-size, so the
	   controls track the user's text-size preference instead of staying at a
	   fixed 36px while the type around them grows. */
  .composer-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.35em;
    font-size: var(--text-base);
    padding: 0.3em;
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: 1.6em;
    transition: border-color var(--transition-fast);
  }
  .composer-row:focus-within {
    border-color: var(--text-dim);
  }

  /* Single line: [+] [text] [mic][send]. Once the text wraps it takes the full
	   width on its own row and the controls fall underneath it — same elements,
	   same handlers, just reordered. */
  .plus {
    order: 0;
  }
  .ta-wrap {
    order: 1;
    position: relative;
    display: flex;
    flex: 1 1 auto;
    min-width: 0;
  }
  .tools {
    order: 2;
    display: flex;
    align-items: center;
    gap: 0.35em;
    margin-left: auto;
  }
  .composer-row.multiline .ta-wrap {
    order: 0;
    flex-basis: 100%;
  }
  .composer-row.multiline .plus {
    order: 1;
  }
  /* Both values live here, not just the active one: the wrap measurement reads
	   the single-row padding by name while the field is in multiline (see
	   wrapsAtSingleRowWidth), so it has to stay resolvable in either state. */
  .composer-row {
    --ta-pad-x-single: 0.25em;
    /* Sized to match the top gap *optically*, which is not the same as matching
		   the box padding: line-height 1.4 puts ~0.2em of half-leading above the
		   text and the cap sits ~0.13em below the em box, so the top reads about
		   0.33em roomier than its 0.45em padding. Setting the sides to 0.45em too
		   left them looking half as wide as the top — most obvious at mobile
		   widths, where the pill is narrow and the text runs to both edges. */
    --ta-pad-x-multi: 0.8em;
    --ta-pad-x: var(--ta-pad-x-single);
  }
  /* On its own full-width row the text no longer starts beside the + button, so
	   without this it runs up against the pill's rounded edge. */
  .composer-row.multiline {
    --ta-pad-x: var(--ta-pad-x-multi);
  }

  textarea {
    flex: 1;
    resize: none;
    overflow-y: auto;
    scrollbar-width: none;
    background: transparent;
    border: none;
    color: var(--text-primary);
    font: inherit;
    line-height: 1.4;
    padding: 0.45em var(--ta-pad-x);
    /* The bordered pill around it carries the focus affordance
       (.composer-row:focus-within), so a ring here would double it. */
    outline: none;
  }
  textarea::-webkit-scrollbar {
    display: none;
  }

  .rec-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    gap: 0.4em;
    padding: 0 0.25em;
    background: var(--surface-raised);
    color: var(--text-secondary);
    border-radius: 1.2em;
    pointer-events: none;
  }
  .rec-dot {
    width: 0.6em;
    height: 0.6em;
    border-radius: 50%;
    background: var(--status-danger-fg);
    animation: rec-pulse 1.2s ease-in-out infinite;
  }
  @keyframes rec-pulse {
    50% {
      opacity: 0.3;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .rec-dot {
      animation: none;
    }
  }

  .icon-btn {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2.35em;
    height: 2.35em;
    border-radius: 50%;
    border: none;
    background: transparent;
    /* A button does not inherit font by default — without this the em sizes
	     above resolve against the UA's ~13px and the controls stay a fixed size
	     while the type around them grows. */
    font: inherit;
    color: var(--text-muted);
    cursor: pointer;
    /* Opts out of double-tap-to-zoom on this target, which is what makes iOS
	     hold a tap open and then deliver it as a delayed synthesized click. */
    touch-action: manipulation;
    transition:
      background var(--transition-fast),
      color var(--transition-fast);
  }
  /* Size the glyph in CSS rather than through lucide's `size` prop, which
	   bakes a px number into the width/height attributes. */
  .icon-btn :global(svg) {
    width: 1.25em;
    height: 1.25em;
  }
  /* Guarded, because iOS Safari synthesizes :hover on tap and leaves it applied
     until a later tap displaces it — an unguarded hover fill is worn by the last
     control the finger touched, for as long as it stays untouched.

     This is the device half of the guard the message row uses, without its
     gesture half (`.msg:not(.touch)`): a touchscreen laptop reports hover, so a
     tap there still strands one. The send button carries the pair anyway, since
     its guarded rule sets only `filter` and cannot contradict the mode. What
     could still strand is the grey fill on the plus and mic — cosmetic, and it
     would take plumbing a touch flag through this component to close. */
  @media (hover: hover) {
    /* `:not(.send)` is load-bearing, not tidiness: this rule outranks
       `.icon-btn.send` by a class, so without the exclusion it would grey out
       the filled control on hover — which is why a `.send:hover` rule used to
       sit below re-asserting the blue, and that re-assertion is what painted
       the *stop* button blue. See the fill note below. */
    .icon-btn:not(.send):hover:not(:disabled) {
      background: var(--surface-badge);
      color: var(--accent-hover);
    }
    /* The filled control brightens instead. A state may adjust the fill but
       never name it, so no state can disagree with the mode. */
    .icon-btn.send:hover:not(:disabled) {
      filter: brightness(1.12);
    }
  }
  .icon-btn:disabled {
    opacity: 0.4;
    cursor: default;
  }
  /* The send affordance is the one filled control in the bar, and its fill is
     its mode: blue means this sends, red means this stops. Exactly two rules
     below set that fill — the two modes — and nothing else may, at any
     specificity. A `:hover` rule that re-declared the blue used to outrank
     `.stop` by one class and paint the running task's stop button blue under
     the stop glyph, which on iOS was not a flicker but the resting state for
     the whole turn.

     No transition either. The fill is the mode rather than a decoration of it,
     so easing it means the colour and the glyph state different things for the
     length of the ease — and a transition is also what invites the compositor
     promotion that leaves WebKit painting a stale layer. Just flip it. */
  .icon-btn.send {
    background: var(--accent-blue);
    color: var(--on-accent-fg);
    transition: none;
  }
  .icon-btn.send:disabled {
    background: var(--surface-badge);
    color: var(--text-muted);
    opacity: 1;
  }
  /* Last of the fill rules on purpose: it ties with the :disabled rule above on
     specificity, so order is what decides them. Unreachable today — the stop
     mode is never disabled — but if that changes, a disabled stop renders as an
     enabled one rather than dropping to grey, which is the lesser wrong of the
     two: a grey button under a stop glyph is the state this control is not
     allowed to be in. Give it its own `.stop:disabled` rule before disabling it. */
  .icon-btn.send.stop {
    background: var(--status-danger-fg);
    color: var(--on-accent-fg);
  }

  .file-hidden {
    display: none;
  }

  /* Above the composer rather than over it, anchored to the attach button's
     edge. `.composer` is the positioned ancestor, and its own padding is what
     the left offset matches so the menu lines up with the button below it. */
  .attach-menu {
    position: absolute;
    bottom: 100%;
    left: max(0.75rem, var(--safe-left));
    margin-bottom: var(--space-1);
    padding: var(--space-1);
    display: flex;
    flex-direction: column;
    min-width: 12rem;
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-md);
    z-index: var(--z-popover);
  }
  .attach-menu-item {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-2);
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    font: inherit;
    font-size: var(--text-sm);
    color: var(--text-primary);
    text-align: left;
    white-space: nowrap;
    cursor: pointer;
  }
  /* Guarded for the same reason the icon buttons above are: iOS synthesizes a
     hover on tap and leaves it applied, so an unguarded rule lights the row
     that was last touched and keeps it lit. */
  @media (hover: hover) {
    .attach-menu-item:hover {
      background: var(--surface-raised);
    }
  }

  .attach-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    margin-bottom: var(--space-2);
  }
  /* The staged citation. Raised fill for the same reason the attachment chips
	   use one — the dock behind it already paints --surface-card — with a
	   leading rule so it reads as a quotation rather than as another chip. */
  .reply-chip {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-2);
    padding: var(--space-1) var(--space-2);
    background: var(--surface-raised);
    border-left: 2px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font-size: var(--text-xs);
    line-height: 1.4;
  }
  .reply-chip :global(.reply-chip-icon) {
    flex: 0 0 auto;
    color: var(--text-dim);
  }
  /* One line: the chip says which message, not what it said. */
  .reply-chip-text {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Raised fill, not --surface-card: the dock behind these now paints the pane
	   fill (which *is* --surface-card in the dark theme), so a card-filled chip
	   would read as bare text on it. Matches the pill below. */
  .attach-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-secondary);
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    padding: 0.15rem var(--space-2);
  }
  .attach-chip.uploading {
    color: var(--text-muted);
  }
  .attach-x {
    display: inline-flex;
    background: none;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    padding: 0;
  }
  @media (hover: hover) {
    .attach-x:hover {
      color: var(--text-primary);
    }
  }
  /* Upload / recorder failures. Chip-shaped like the attachment row above it:
	   floating over the transcript, bare colored text had no backdrop of its own
	   and read straight through to whatever message sat behind it. */
  .notice-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    margin-bottom: var(--space-2);
  }
  .attach-error {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    background: var(--status-danger-bg);
    border: 1px solid color-mix(in srgb, var(--status-danger-fg) 45%, transparent);
    border-radius: var(--radius-pill);
    padding: 0.15rem var(--space-2);
  }
</style>
