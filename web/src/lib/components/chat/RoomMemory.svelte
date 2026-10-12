<script lang="ts">
  import TextFileEditor from '$lib/components/TextFileEditor.svelte';
  import {
    getRoomMemory,
    saveRoomMemory,
    getRoomNotes,
    saveRoomNotes,
    ChatMemoryBusyError,
    type ChatRoomMemory,
    type ChatRoomNotes,
  } from '$lib/api';

  interface Props {
    open?: boolean;
    roomId: number;
    roomName: string;
    /** `room`: the room's CHANNEL.md, read by everyone in it. `mine`: the
     * caller's own notes about the room, read only into their own turns. */
    kind?: 'room' | 'mine';
    onClose: () => void;
  }

  let { open = $bindable(false), roomId, roomName, kind = 'room', onClose }: Props = $props();

  type Loaded = ChatRoomNotes & Partial<Pick<ChatRoomMemory, 'shared' | 'template'>>;

  const mine = $derived(kind === 'mine');
  let loaded = $state<Loaded | null>(null);

  function load(id: string | number): Promise<Loaded> {
    return mine ? getRoomNotes(Number(id)) : getRoomMemory(Number(id));
  }

  function save(id: string | number, content: string, revision: string) {
    return (mine ? saveRoomNotes : saveRoomMemory)(Number(id), content, revision);
  }

  function describeError(e: unknown): string | null {
    return e instanceof ChatMemoryBusyError
      ? `${e.message} — memory can't be saved while the room is working.`
      : null;
  }

  const title = $derived(mine ? 'My notes' : loaded?.shared ? 'Room notes' : 'Room memory');
  const description = $derived(
    mine
      ? `Your private notes about ${roomName}.`
      : `Standing notes ${roomName} is given at the start of every message.`,
  );
</script>

<TextFileEditor
  bind:open
  bind:loaded
  fileKey={roomId}
  {title}
  {description}
  ariaLabel={mine ? 'My notes (markdown)' : 'Room memory (markdown)'}
  placeholder={mine ? 'No notes yet.' : 'No memory yet for this room.'}
  loadFailed={mine ? "Couldn't load your notes." : "Couldn't load this room's memory."}
  {load}
  {save}
  {describeError}
  {onClose}
>
  {#snippet intro(file)}
    {#if mine}
      <p class="caption shared">
        Only you can see these. The bot reads them when it answers you in this room, and when it
        drafts a reply to a guest for you to approve, if you host.
      </p>
    {:else if file.shared}
      <p class="caption shared">Everyone in this room can read and edit these.</p>
    {/if}
  {/snippet}
  {#snippet caption()}
    {#if mine}
      Markdown. Saved in your workspace, never shown to the room.
    {:else}
      Markdown. Saved to <code>CHANNEL.md</code> and read into every task in this room.
    {/if}
  {/snippet}
</TextFileEditor>

<style>
  p.caption {
    margin: var(--space-1) 0 0;
  }
  .shared {
    margin-bottom: var(--space-2);
  }
</style>
