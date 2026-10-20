<script lang="ts">
  /**
   * A new note in the quick notes folder: a text box with focus and a Done
   * button, no title. The file is named by the server from the first line.
   *
   * Saves are serialised through one promise chain, so Done, the idle timer and
   * the page being hidden can fire together and still make one create followed
   * by replacements. The create carries a `client_note_id` minted once per
   * editor session and kept in the draft, so a retry after a lost response
   * replays to the same file instead of writing a second one.
   *
   * The draft in localStorage is the safety net: it is written on every input
   * and cleared only once the server has confirmed the current text and the
   * user pressed Done. An expired session goes to the login page with
   * `?next=notes-new` (the layout's `expireSession`), and the draft brings the
   * text back on return.
   */
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { onDestroy, onMount, tick } from 'svelte';
  import {
    AuthError,
    QuickNoteError,
    TextFileConflictError,
    TextFileLockedError,
    createQuickNote,
    saveQuickNote,
    updateProfile,
  } from '$lib/api';
  import { Alert, AppShell, Button, ShellHeader, TextArea } from '$lib/components/ui';
  import FolderPicker from '$lib/components/FolderPicker.svelte';
  import { getCurrentUser } from '$lib/userContext';
  import { syncDeviceActions } from '$lib/stores/deviceActions';
  import { debounce } from '$lib/ui/debounce';
  import { loadSetting, saveSetting } from '$lib/ui/persisted';

  const DRAFT_KEY = 'notes.newDraft';
  const IDLE_MS = 1500;
  // The server's pattern for the key; a draft carrying anything else is not ours.
  const CLIENT_NOTE_ID = /^[A-Za-z0-9-]{8,64}$/;

  interface Draft {
    text: string;
    clientNoteId: string;
    path: string | null;
    revision: string | null;
  }

  /**
   * What stops the editor saving. `error` is a failed attempt the next save
   * retries; the others hold every save until the user acts on them, since
   * retrying would only be refused again or would overwrite someone else.
   */
  type Problem = {
    kind: 'error' | 'conflict' | 'moved' | 'folder-changed' | 'folder-unset';
    message: string;
  };

  const identity = getCurrentUser();

  function readDraft(): Draft | null {
    const raw = loadSetting<unknown>(DRAFT_KEY, null);
    if (!raw || typeof raw !== 'object') return null;
    const d = raw as Record<string, unknown>;
    if (typeof d.text !== 'string') return null;
    if (typeof d.clientNoteId !== 'string' || !CLIENT_NOTE_ID.test(d.clientNoteId)) return null;
    return {
      text: d.text,
      clientNoteId: d.clientNoteId,
      path: typeof d.path === 'string' && d.path ? d.path : null,
      revision: typeof d.revision === 'string' ? d.revision : null,
    };
  }

  function clearDraft() {
    try {
      localStorage.removeItem(DRAFT_KEY);
    } catch {
      // Storage refused: the draft will be overwritten by the next one.
    }
  }

  function newClientNoteId(): string {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    // randomUUID needs a secure context; getRandomValues does not.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }

  function fileName(of: string): string {
    return of.split('/').pop() ?? of;
  }

  const restored = readDraft();
  let text = $state(restored?.text ?? '');
  let clientNoteId = restored?.clientNoteId ?? newClientNoteId();
  let path: string | null = $state(restored?.path ?? null);
  let name = $state(restored?.path ? fileName(restored.path) : '');
  let revision: string | null = restored?.revision ?? null;
  // The text the server is known to hold for this note. Null after a restore:
  // the draft cannot say whether its last save landed.
  let lastSavedText: string | null = null;

  let problem: Problem | null = $state(null);
  let saving = $state(false);
  let finished = $state(false);
  let pickingFolder = $state(false);
  let folderBusy = $state(false);
  let ref: HTMLTextAreaElement | undefined = $state();

  let chain: Promise<void> = Promise.resolve();
  // Bumped when the editor starts over, so an answer for the old note is dropped.
  let session = 0;

  const folder = $derived(path ? path.split('/').slice(0, -1).join('/') : '');
  const filesHref = $derived(
    `${base}/files?${new URLSearchParams({ place: 'home', path: folder })}`,
  );

  function writeDraft() {
    saveSetting<Draft>(DRAFT_KEY, { text, clientNoteId, path, revision });
  }

  const idle = debounce(() => void save(), IDLE_MS);

  function save(): Promise<void> {
    idle.cancel();
    chain = chain.then(runSave);
    return chain;
  }

  function describe(e: unknown): Problem {
    if (e instanceof TextFileConflictError) {
      return { kind: 'conflict', message: 'This note was changed elsewhere.' };
    }
    if (e instanceof TextFileLockedError) {
      return { kind: 'error', message: 'The note is being saved elsewhere. Try again shortly.' };
    }
    if (e instanceof QuickNoteError) {
      if (e.code === 'notes_folder_unset') {
        return { kind: 'folder-unset', message: 'Choose a quick notes folder to save this note.' };
      }
      if (e.code === 'outside_folder') {
        return {
          kind: 'folder-changed',
          message: 'Your quick notes folder changed since this note was saved.',
        };
      }
      if (e.code === 'missing') {
        return { kind: 'moved', message: 'This note was moved or deleted.' };
      }
      if (e.code === 'in_progress') {
        return { kind: 'error', message: 'This note is still being saved. Try again shortly.' };
      }
    }
    const message = e instanceof Error && e.message ? e.message : 'Could not save the note.';
    return { kind: 'error', message };
  }

  /** One save. Never rejects: the chain behind it has to keep running. */
  async function runSave(): Promise<void> {
    if (problem && problem.kind !== 'error') return;
    const content = text;
    if (content === lastSavedText) return;
    if (path === null && !content.trim()) return;
    const mine = session;
    saving = true;
    try {
      if (path === null) {
        const note = await createQuickNote(content, clientNoteId);
        if (mine !== session) return;
        path = note.path;
        name = note.name;
        revision = note.revision;
        if (note.created) {
          lastSavedText = content;
        } else if (note.revision !== null) {
          // A replay: the file this key wrote, perhaps edited since. Put this
          // text over it against the revision it has now.
          const saved = await saveQuickNote(note.path, content, note.revision);
          if (mine !== session) return;
          revision = saved.revision;
          lastSavedText = content;
        }
      } else if (revision !== null) {
        const saved = await saveQuickNote(path, content, revision);
        if (mine !== session) return;
        revision = saved.revision;
        lastSavedText = content;
      }
      // A replay with no revision, or a restored draft without one: the file
      // the key wrote is gone, and a save never re-creates it.
      problem =
        revision === null ? { kind: 'moved', message: 'This note was moved or deleted.' } : null;
    } catch (e) {
      if (mine !== session) return;
      if (e instanceof AuthError) {
        writeDraft();
        identity.expireSession();
        return;
      }
      problem = describe(e);
    } finally {
      if (mine === session) {
        saving = false;
        writeDraft();
      }
    }
  }

  function edited(event: Event) {
    text = (event.currentTarget as HTMLTextAreaElement).value;
    writeDraft();
    idle();
  }

  async function done() {
    await save();
    // Typing while the save was out leaves text the server does not have yet.
    if (!problem && text !== lastSavedText && path !== null) await save();
    if (problem) return;
    if (path === null) {
      if (text.trim()) return;
      clearDraft();
      close();
      return;
    }
    if (text !== lastSavedText) return;
    clearDraft();
    finished = true;
  }

  /** Start a new note from this text, under a fresh key: the old file stays as it is. */
  function saveAsNew() {
    session++;
    clientNoteId = newClientNoteId();
    path = null;
    name = '';
    revision = null;
    lastSavedText = null;
    problem = null;
    writeDraft();
    void save();
  }

  async function newNote() {
    session++;
    clearDraft();
    text = '';
    clientNoteId = newClientNoteId();
    path = null;
    name = '';
    revision = null;
    lastSavedText = null;
    problem = null;
    finished = false;
    await tick();
    ref?.focus();
  }

  function close() {
    if (history.length > 1) history.back();
    else void goto(`${base}/`);
  }

  async function useFolder(chosen: string) {
    folderBusy = true;
    try {
      await updateProfile({ quick_notes_folder: chosen });
    } catch (e) {
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      const message = e instanceof Error && e.message ? e.message : 'Could not save the folder.';
      problem = { kind: 'folder-unset', message };
      return;
    } finally {
      folderBusy = false;
    }
    problem = null;
    void syncDeviceActions();
    await save();
  }

  function onVisibility() {
    if (document.visibilityState !== 'hidden') return;
    writeDraft();
    void save();
  }

  onMount(() => {
    // `autofocus` covers a full load; a client-side navigation needs the call.
    ref?.focus();
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  });

  onDestroy(() => idle.cancel());
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader title="New note">
      {#snippet tools()}
        {#if !finished}
          <Button variant="primary" size="sm" onclick={done} disabled={folderBusy}>Done</Button>
        {/if}
      {/snippet}
    </ShellHeader>
  {/snippet}

  <div class="note-pane">
    {#if finished}
      <div class="saved vstack">
        <p>Saved as <strong>{name}</strong> in {folder}</p>
        <div class="hstack wrap saved-actions">
          <Button variant="secondary" href={filesHref}>Show in Files</Button>
          <Button variant="primary" onclick={newNote}>New note</Button>
          <Button variant="secondary" onclick={close}>Close</Button>
        </div>
      </div>
    {:else}
      {#if problem}
        <Alert
          appearance="inline"
          variant={problem.kind === 'error' ? 'danger' : 'warn'}
          role="alert"
          class="note-problem"
        >
          {problem.message}
          {#if problem.kind !== 'error' && problem.kind !== 'folder-unset'}
            Your text is kept here.
          {/if}
          {#snippet actions()}
            {#if problem?.kind === 'folder-unset'}
              <Button
                size="sm"
                variant="primary"
                loading={folderBusy}
                onclick={() => (pickingFolder = true)}>Choose folder</Button
              >
            {:else if problem && problem.kind !== 'error'}
              <Button size="sm" variant="secondary" onclick={saveAsNew}>Save as a new note</Button>
            {/if}
          {/snippet}
        </Alert>
      {/if}
      <!-- svelte-ignore a11y_autofocus -->
      <TextArea
        bind:value={text}
        bind:ref
        class="note-editor"
        aria-label="Note"
        aria-busy={saving || undefined}
        placeholder="Write a note. The first line names it."
        autofocus
        oninput={edited}
      />
    {/if}
  </div>
</AppShell>

<FolderPicker
  bind:open={pickingFolder}
  title="Quick notes folder"
  description="New notes are saved in the folder you choose."
  onPick={(chosen) => void useFolder(chosen)}
/>

<style>
  .note-pane {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    height: 100%;
    box-sizing: border-box;
    padding: var(--space-4);
  }
  .note-pane :global(.note-editor) {
    flex: 1;
    min-height: 12rem;
    resize: none;
    font-size: var(--text-base);
    line-height: 1.5;
  }
  .saved p {
    margin: 0;
  }
  .saved-actions {
    gap: var(--space-2);
  }
</style>
