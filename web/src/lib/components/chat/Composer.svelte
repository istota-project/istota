<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import { ArrowUp, Square, Plus, Mic, Check, Trash2, X } from 'lucide-svelte';
  import { uploadChatAttachment, type ChatAttachment } from '$lib/api';
  import AutocompletePopover from './autocomplete/AutocompletePopover.svelte';
  import { createAutocomplete, type AcceptResult } from './autocomplete/useAutocomplete.svelte';
  import { commandProvider, modelAliasProvider } from './autocomplete/providers';
  import { createRecorder, formatElapsed } from './useRecorder.svelte';
  import { usesSoftKeyboard } from '$lib/platform/input';

  let {
    onSend,
    onCancel,
    busy = false,
    placeholder = 'Message Istota…',
  }: {
    onSend: (text: string, attachments: { path: string; name: string }[]) => void;
    onCancel?: () => void;
    busy?: boolean;
    placeholder?: string;
  } = $props();

  let text = $state('');
  let textarea: HTMLTextAreaElement | undefined = $state();
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
  // True once the text no longer fits on one line: the controls drop below the
  // field instead of sharing its row. Driven by the measurement in autoGrow,
  // not a media query, so it tracks content rather than viewport width.
  let multiline = $state(false);

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

  async function upload(files: FileList | File[]) {
    uploadError = '';
    for (const file of Array.from(files)) {
      uploading++;
      try {
        const att = await uploadChatAttachment(file);
        attachments = [...attachments, att];
      } catch (e) {
        uploadError = e instanceof Error ? e.message : 'upload failed';
      } finally {
        uploading--;
      }
    }
  }

  function removeAttachment(path: string) {
    attachments = attachments.filter((a) => a.path !== path);
  }

  function submit() {
    const t = text.trim();
    if (!t && attachments.length === 0) return;
    onSend(
      t,
      attachments.map((a) => ({ path: a.path, name: a.name })),
    );
    text = '';
    attachments = [];
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
      submit();
      return;
    }
    // The engine consumes Arrow/Tab/Enter/Escape only while the popover is
    // open; when closed it returns false and the key does what it says.
    if (ac.onKeydown(e)) {
      e.preventDefault();
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
     toggle has to re-evaluate it; nothing else fires while the text is idle. -->
<svelte:window onresize={autoGrow} />

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
      onclick={() => fileInput?.click()}
      type="button"
      aria-label="Attach file"
      title="Attach file"
    >
      <Plus />
    </button>
    <input
      bind:this={fileInput}
      type="file"
      multiple
      class="file-hidden"
      onchange={(e) => {
        const f = (e.target as HTMLInputElement).files;
        if (f) upload(f);
        (e.target as HTMLInputElement).value = '';
      }}
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
    padding: 0.6rem 0.75rem;
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
  .icon-btn:hover:not(:disabled) {
    background: var(--surface-badge);
    color: var(--accent-hover);
  }
  .icon-btn:disabled {
    opacity: 0.4;
    cursor: default;
  }
  /* The send affordance is the one filled control in the bar. */
  .icon-btn.send {
    background: var(--accent-blue);
    color: var(--on-accent-fg);
  }
  .icon-btn.send:hover:not(:disabled) {
    background: var(--accent-blue);
    color: var(--on-accent-fg);
    filter: brightness(1.12);
  }
  .icon-btn.send:disabled {
    background: var(--surface-badge);
    color: var(--text-muted);
    opacity: 1;
  }
  .icon-btn.send.stop {
    background: var(--status-danger-fg);
    color: var(--on-accent-fg);
  }

  .file-hidden {
    display: none;
  }

  .attach-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-bottom: 0.4rem;
  }
  /* Raised fill, not --surface-card: the dock behind these now paints the pane
	   fill (which *is* --surface-card in the dark theme), so a card-filled chip
	   would read as bare text on it. Matches the pill below. */
  .attach-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--text-xs);
    color: var(--text-secondary);
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    padding: 0.15rem 0.45rem;
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
  .attach-x:hover {
    color: var(--text-primary);
  }
  /* Upload / recorder failures. Chip-shaped like the attachment row above it:
	   floating over the transcript, bare colored text had no backdrop of its own
	   and read straight through to whatever message sat behind it. */
  .notice-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-bottom: 0.4rem;
  }
  .attach-error {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    background: var(--status-danger-bg);
    border: 1px solid color-mix(in srgb, var(--status-danger-fg) 45%, transparent);
    border-radius: var(--radius-pill);
    padding: 0.15rem 0.45rem;
  }
</style>
