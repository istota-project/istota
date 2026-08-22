<script lang="ts">
  import { Modal, Button, TextArea } from '$lib/components/ui';
  import {
    getRoomMemory,
    saveRoomMemory,
    ChatMemoryConflictError,
    ChatMemoryBusyError,
    type ChatRoomMemory,
  } from '$lib/api';

  interface Props {
    open?: boolean;
    roomId: number;
    roomName: string;
    onClose: () => void;
  }

  let { open = $bindable(false), roomId, roomName, onClose }: Props = $props();

  let loading = $state(false);
  let saving = $state(false);
  let error = $state('');
  // Set only by a conflict, which needs a different recovery from an ordinary
  // error: the user's text is still in the box and must not be thrown away.
  let conflicted = $state(false);
  let saved = $state(false);
  let loaded = $state<ChatRoomMemory | null>(null);
  let text = $state('');
  let revision = $state('');
  // The id the current `loaded` belongs to, so reopening the modal on another
  // room can't show the previous room's file while the fetch is in flight.
  let loadedRoomId = $state<number | null>(null);

  const dirty = $derived(loaded !== null && text !== loaded.content);
  const empty = $derived(loaded !== null && !loaded.exists && !dirty);

  async function load() {
    loading = true;
    error = '';
    conflicted = false;
    saved = false;
    // Drop the previous room's payload *before* awaiting. Keeping it across a
    // failed load renders room A's file under room B's title, and a save then
    // posts A's text to B carrying A's revision — which the server accepts
    // whenever both were empty, since sha256('') matches on either side.
    loaded = null;
    loadedRoomId = null;
    text = '';
    revision = '';
    const forRoom = roomId;
    try {
      const data = await getRoomMemory(forRoom);
      // A slow response for a room the user has since navigated away from is
      // dropped rather than rendered.
      if (forRoom !== roomId) return;
      loaded = data;
      loadedRoomId = forRoom;
      text = data.content;
      revision = data.revision;
    } catch {
      if (forRoom !== roomId) return;
      error = "Couldn't load this room's memory.";
    } finally {
      if (forRoom === roomId) loading = false;
    }
  }

  // Fetch on every open, not only on a room change: the file is written by the
  // agent between visits, so a cached buffer would carry a stale revision and
  // the first save would report the assistant as having changed the file "while
  // you were editing" — pointing at a write that predates this session.
  let openedFor = $state<number | null>(null);
  $effect(() => {
    if (!open) {
      openedFor = null;
      return;
    }
    if (openedFor !== roomId) {
      openedFor = roomId;
      load();
    }
  });

  function useTemplate() {
    if (!loaded) return;
    text = loaded.template;
  }

  async function handleSave() {
    // `loadedRoomId` rather than `roomId`: the revision in hand belongs to the
    // room that was loaded, so saving against any other room is a cross-room
    // write however the two came to disagree.
    if (saving || !loaded || loadedRoomId !== roomId) return;
    saving = true;
    error = '';
    conflicted = false;
    saved = false;
    try {
      const res = await saveRoomMemory(loadedRoomId, text, revision);
      revision = res.revision;
      loaded = { ...loaded, content: text, exists: text.trim().length > 0 };
      saved = true;
    } catch (e) {
      if (e instanceof ChatMemoryConflictError) {
        conflicted = true;
      } else if (e instanceof ChatMemoryBusyError) {
        error = `${e.message} — memory can't be saved while the room is working.`;
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

<Modal
  bind:open
  title="Room memory"
  description={`Standing notes ${roomName} is given at the start of every message.`}
  onOpenChange={handleOpenChange}
  width="620px"
>
  {#if loading}
    <p class="caption">Loading…</p>
  {:else if loaded === null}
    <p class="msg-error">{error || "Couldn't load this room's memory."}</p>
  {:else}
    {#if loaded.shared}
      <p class="caption shared">
        This room is shared. Everyone in it reads and writes the same memory.
      </p>
    {/if}

    <TextArea
      bind:value={text}
      rows={18}
      monospace
      spellcheck="false"
      aria-label="Room memory (markdown)"
      placeholder={empty ? 'No memory yet for this room.' : ''}
    />

    <div class="row">
      <p class="caption">
        Markdown. Saved to <code>CHANNEL.md</code> and read into every task in this room.
      </p>
      {#if empty}
        <button class="link-btn" type="button" onclick={useTemplate}> Start from template </button>
      {/if}
    </div>

    {#if conflicted}
      <p class="msg-warn">
        Someone — probably the assistant — changed this file while you were editing. Your text is
        still here.
        <button class="link-btn" type="button" onclick={load}>Discard mine and reload</button>
      </p>
    {:else if error}
      <p class="msg-error">{error}</p>
    {:else if saved && !dirty}
      <p class="caption saved">Saved.</p>
    {/if}
  {/if}

  {#snippet footer()}
    <Button variant="ghost" onclick={onClose}>Close</Button>
    <Button variant="primary" onclick={handleSave} disabled={!dirty || saving || loading}>
      {saving ? 'Saving…' : 'Save'}
    </Button>
  {/snippet}
</Modal>

<style>
  p.caption {
    margin: var(--space-1) 0 0;
  }

  .row {
    display: flex;
    gap: var(--space-2);
    align-items: baseline;
    justify-content: space-between;
  }

  .shared {
    margin-bottom: var(--space-2);
  }

  .saved {
    color: var(--status-success-fg);
  }

  .msg-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    margin: var(--space-2) 0 0;
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
