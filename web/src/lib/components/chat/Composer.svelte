<script lang="ts">
  import { tick } from 'svelte';
  import { SendHorizontal, Square, Paperclip, X } from 'lucide-svelte';
  import { uploadChatAttachment, type ChatAttachment } from '$lib/api';
  import AutocompletePopover from './autocomplete/AutocompletePopover.svelte';
  import { createAutocomplete, type AcceptResult } from './autocomplete/useAutocomplete.svelte';
  import { commandProvider, modelAliasProvider } from './autocomplete/providers';

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
  let attachments = $state<ChatAttachment[]>([]);
  let uploading = $state(0);
  let dragOver = $state(false);
  let uploadError = $state('');

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

  function autoGrow() {
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
  }

  // iOS Safari auto-zooms when a focused input renders below 16px. Rather than
  // inflating the field's font (which throws off its height vs. the buttons),
  // pin the viewport's maximum-scale only while the textarea is focused, then
  // restore it on blur so pinch-to-zoom keeps working everywhere else.
  const VIEWPORT_DEFAULT = 'width=device-width, initial-scale=1';
  const VIEWPORT_NO_ZOOM = 'width=device-width, initial-scale=1, maximum-scale=1';

  function setViewport(content: string) {
    const meta = document.querySelector('meta[name="viewport"]');
    if (meta) meta.setAttribute('content', content);
  }
  function onFocus() {
    setViewport(VIEWPORT_NO_ZOOM);
  }
  function onBlur() {
    setViewport(VIEWPORT_DEFAULT);
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
    // The engine consumes Arrow/Tab/Enter/Escape only while the popover is
    // open; when closed it returns false and Enter-to-send runs as before.
    if (ac.onKeydown(e)) {
      e.preventDefault();
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
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
          📎 {att.name}
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
  {#if uploadError}<div class="attach-error">{uploadError}</div>{/if}

  <div class="composer-row">
    <button
      class="icon-btn"
      onclick={() => fileInput?.click()}
      type="button"
      aria-label="Attach file"
      title="Attach file"
    >
      <Paperclip size={16} />
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
    <div class="ta-wrap">
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
        aria-label="Message"
        role="combobox"
        aria-expanded={ac.open}
        aria-controls={AC_LIST_ID}
        aria-autocomplete="list"
        aria-activedescendant={acActiveDescendant}
      ></textarea>
    </div>
    {#if busy && onCancel}
      <button
        class="icon-btn send stop"
        onclick={onCancel}
        type="button"
        aria-label="Stop"
        title="Stop"
      >
        <Square size={16} />
      </button>
    {:else}
      <button
        class="icon-btn send"
        onclick={submit}
        type="button"
        disabled={!text.trim() && attachments.length === 0}
        aria-label="Send"
        title="Send"
      >
        <SendHorizontal size={16} />
      </button>
    {/if}
  </div>
</div>

<style>
  .composer {
    padding: 0.6rem 0.75rem;
    border-top: 1px solid var(--border-subtle);
    background: var(--surface-card);
  }
  .composer.drag {
    background: var(--surface-raised);
    outline: 1px dashed var(--border-default);
  }
  .composer-row {
    display: flex;
    align-items: flex-end;
    gap: 0.5rem;
  }
  .ta-wrap {
    position: relative;
    flex: 1;
    display: flex;
  }

  textarea {
    flex: 1;
    resize: none;
    max-height: 200px;
    overflow-y: auto;
    scrollbar-width: none;
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-base);
    line-height: 1.4;
    padding: 0.5rem 0.65rem;
    outline: none;
  }
  textarea::-webkit-scrollbar {
    display: none;
  }
  textarea:focus {
    border-color: var(--text-dim);
  }

  .icon-btn {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: var(--radius-card);
    border: 1px solid var(--border-default);
    background: var(--surface-raised);
    color: var(--text-muted);
    cursor: pointer;
    transition:
      background var(--transition-fast),
      color var(--transition-fast);
  }
  .icon-btn:hover:not(:disabled) {
    background: var(--surface-badge);
    color: var(--accent-hover);
  }
  .icon-btn:disabled {
    opacity: 0.4;
    cursor: default;
  }
  .icon-btn.send {
    color: var(--text-primary);
  }
  .icon-btn.stop {
    color: #e0a0a0;
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
  .attach-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--text-xs);
    color: var(--text-secondary);
    background: var(--surface-card);
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
  .attach-error {
    color: #e0a0a0;
    font-size: var(--text-xs);
    margin-bottom: 0.3rem;
  }

  /* Light theme overrides — dark rules above untouched. */
  :global(:root[data-theme='light']) .composer {
    background: #ffffff;
  }
  :global(:root[data-theme='light']) .icon-btn.stop {
    color: #c0271d;
  }
  :global(:root[data-theme='light']) .attach-error {
    color: #c0271d;
  }
</style>
