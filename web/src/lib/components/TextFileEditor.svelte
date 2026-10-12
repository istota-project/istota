<script lang="ts" generics="T extends TextFile">
  import type { Snippet } from 'svelte';
  import { Modal, Button, TextArea } from '$lib/components/ui';
  import { TextFileConflictError, TextFileInvalidError, TextFileLockedError } from '$lib/api';
  import type { TextFile } from './textFile';

  interface Props {
    open?: boolean;
    /** Which file. A change, or a reopen, reloads it. */
    fileKey: string | number;
    title: string;
    description: string;
    ariaLabel: string;
    /** Shown in the empty box while the file is absent and untouched. */
    placeholder?: string;
    loadFailed: string;
    load: (key: string | number) => Promise<T>;
    save: (
      key: string | number,
      content: string,
      revision: string,
    ) => Promise<{ revision: string; note?: string }>;
    /** The loaded payload, for a caller whose title or captions read it. */
    loaded?: T | null;
    /** A caller's wording for an error only it knows about; null falls through. */
    describeError?: (e: unknown) => string | null;
    intro?: Snippet<[T]>;
    caption?: Snippet<[T]>;
    onClose: () => void;
  }

  let {
    open = $bindable(false),
    fileKey,
    title,
    description,
    ariaLabel,
    placeholder = '',
    loadFailed,
    load: loadFile,
    save: saveFile,
    loaded = $bindable(null),
    describeError,
    intro,
    caption,
    onClose,
  }: Props = $props();

  let loading = $state(false);
  let saving = $state(false);
  let error = $state('');
  // Set only by a conflict, which needs a different recovery from an ordinary
  // error: the user's text is still in the box and must not be thrown away.
  let conflicted = $state(false);
  let invalid = $state<string[]>([]);
  let savedNote = $state('');
  let text = $state('');
  let revision = $state('');
  // The key the current `loaded` belongs to, so reopening on another file
  // can't show the previous file while the fetch is in flight.
  let loadedKey = $state<string | number | null>(null);

  const dirty = $derived(loaded !== null && text !== loaded.content);
  const empty = $derived(loaded !== null && !loaded.exists && !dirty);

  function clearStatus() {
    error = '';
    conflicted = false;
    invalid = [];
    savedNote = '';
  }

  async function load() {
    loading = true;
    clearStatus();
    // Drop the previous file's payload *before* awaiting. Keeping it across a
    // failed load renders file A under file B's title, and a save then posts
    // A's text to B carrying A's revision — which the server accepts whenever
    // both were empty, since sha256('') matches on either side.
    loaded = null;
    loadedKey = null;
    text = '';
    revision = '';
    const forKey = fileKey;
    try {
      const data = await loadFile(forKey);
      // A slow response for a file the user has since moved away from is
      // dropped rather than rendered.
      if (forKey !== fileKey) return;
      loaded = data;
      loadedKey = forKey;
      text = data.content;
      revision = data.revision;
    } catch {
      if (forKey !== fileKey) return;
      error = loadFailed;
    } finally {
      if (forKey === fileKey) loading = false;
    }
  }

  // Fetch on every open, not only on a key change: the agent writes these
  // files between visits, so a cached buffer would carry a stale revision and
  // the first save would report a conflict with a write that predates this
  // session.
  let openedFor = $state<string | number | null>(null);
  $effect(() => {
    if (!open) {
      openedFor = null;
      return;
    }
    if (openedFor !== fileKey) {
      openedFor = fileKey;
      load();
    }
  });

  function useTemplate() {
    if (!loaded?.template) return;
    text = loaded.template;
  }

  async function handleSave() {
    // `loadedKey` rather than `fileKey`: the revision in hand belongs to the
    // file that was loaded, so saving against any other is a cross-file write
    // however the two came to disagree.
    if (saving || !loaded || loadedKey !== fileKey) return;
    saving = true;
    clearStatus();
    try {
      const res = await saveFile(loadedKey, text, revision);
      revision = res.revision;
      loaded = { ...loaded, content: text, exists: text.trim().length > 0 };
      savedNote = res.note || 'Saved.';
    } catch (e) {
      const own = describeError?.(e) ?? null;
      if (own !== null) {
        error = own;
      } else if (e instanceof TextFileConflictError) {
        conflicted = true;
      } else if (e instanceof TextFileInvalidError) {
        invalid = e.errors;
      } else if (e instanceof TextFileLockedError) {
        error = 'This file is being written right now. Try again in a moment.';
      } else {
        error = e instanceof Error ? e.message : 'Save failed.';
      }
    } finally {
      saving = false;
    }
  }

  function handleOpenChange(next: boolean) {
    if (!next) onClose();
  }
</script>

<Modal bind:open variant="viewer" {title} {description} onOpenChange={handleOpenChange}>
  <div class="text-file-editor">
    {#if loading}
      <p class="caption">Loading…</p>
    {:else if loaded === null}
      <p class="msg-error">{error || loadFailed}</p>
    {:else}
      {#if intro}{@render intro(loaded)}{/if}

      <TextArea
        bind:value={text}
        rows={18}
        monospace
        spellcheck="false"
        aria-label={ariaLabel}
        placeholder={empty ? placeholder : ''}
      />

      <div class="row">
        <p class="caption">
          {#if caption}{@render caption(loaded)}{/if}
        </p>
        {#if empty && loaded.template}
          <button class="link-btn" type="button" onclick={useTemplate}>
            Start from template
          </button>
        {/if}
      </div>

      {#if conflicted}
        <p class="msg-warn">
          Someone — probably the assistant — changed this file while you were editing. Your text is
          still here.
          <button class="link-btn" type="button" onclick={load}>Discard mine and reload</button>
        </p>
      {:else if invalid.length}
        <div class="msg-error" role="alert">
          <p>Not saved. The assistant would not load this file as written:</p>
          <ul>
            {#each invalid as message}
              <li>{message}</li>
            {/each}
          </ul>
        </div>
      {:else if error}
        <p class="msg-error">{error}</p>
      {:else if savedNote && !dirty}
        <p class="caption saved">{savedNote}</p>
      {/if}
    {/if}
  </div>

  {#snippet footer()}
    <Button variant="ghost" onclick={onClose}>Close</Button>
    <Button variant="primary" onclick={handleSave} disabled={!dirty || saving || loading}>
      {saving ? 'Saving…' : 'Save'}
    </Button>
  {/snippet}
</Modal>

<style>
  .text-file-editor {
    height: 100%;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-height: 0;
  }
  .text-file-editor :global(textarea) {
    flex: 1;
    min-height: 0;
    resize: none;
  }
  .text-file-editor > :not(:global(textarea)) {
    flex-shrink: 0;
  }

  p.caption {
    margin: var(--space-1) 0 0;
  }

  .row {
    display: flex;
    gap: var(--space-2);
    align-items: baseline;
    justify-content: space-between;
  }

  .saved {
    color: var(--status-success-fg);
  }

  .msg-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    margin: var(--space-2) 0 0;
  }
  .msg-error p,
  .msg-error ul {
    margin: 0;
  }
  .msg-error ul {
    padding-left: var(--space-4);
  }

  .msg-warn {
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
    margin: var(--space-2) 0 0;
  }

  .link-btn {
    background: none;
    border: none;
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    text-decoration: underline;
    cursor: pointer;
    padding: 0;
    white-space: nowrap;
  }
  .link-btn:hover {
    color: var(--text-primary);
  }
</style>
