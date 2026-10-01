<script lang="ts">
  import {
    addRoomMember,
    getChatUsers,
    getRoomMembers,
    removeRoomMember,
    type DirectoryUser,
    type RoomMembers,
  } from '$lib/api';
  import { Button, ConfirmDialog, Select, type SelectOption } from '$lib/components/ui';

  interface Props {
    roomId: number;
    /** The viewer's own user id, so their row offers Leave rather than Remove. */
    userId?: string;
    /** A Talk-backed room takes its membership from Talk, which the server
     *  enforces; this only lets the pane say so instead of offering nothing. */
    talkBound?: boolean;
    /** Membership changed: the room's sharing state may have too. */
    onChanged?: () => void;
    /** The viewer left the room, which is no longer theirs to show. */
    onLeft?: () => void;
  }

  let { roomId, userId, talkBound = false, onChanged, onLeft }: Props = $props();

  let data = $state<RoomMembers | null>(null);
  let directory = $state<DirectoryUser[]>([]);
  let error = $state('');
  let busy = $state(false);
  let pick = $state('');
  let confirmAdd = $state(false);

  async function load() {
    error = '';
    const forRoom = roomId;
    try {
      const members = await getRoomMembers(forRoom);
      if (forRoom !== roomId) return;
      data = members;
      if (members.can_manage) {
        const { users } = await getChatUsers();
        if (forRoom === roomId) directory = users;
      }
    } catch {
      if (forRoom === roomId) error = 'Couldn’t load this room’s members.';
    }
  }

  $effect(() => {
    void roomId;
    data = null;
    directory = [];
    pick = '';
    load();
  });

  const candidates = $derived<SelectOption[]>([
    { value: '', label: 'Add someone…' },
    ...directory
      .filter((u) => !data?.members.some((m) => m.user_id === u.user_id))
      .map((u) => ({ value: u.user_id, label: u.display_name })),
  ]);
  const pickedName = $derived(directory.find((u) => u.user_id === pick)?.display_name ?? pick);
  const count = $derived(data?.message_count ?? 0);
  // The add discloses the whole transcript, so the dialog states how much.
  const disclosure = $derived(
    `${pickedName} will see all ${count} message${count === 1 ? '' : 's'} in this room, ` +
      'everything said here before they joined included.',
  );

  async function run(action: () => Promise<unknown>) {
    busy = true;
    error = '';
    try {
      await action();
      await load();
      onChanged?.();
    } catch (e) {
      error = e instanceof Error ? e.message : 'That didn’t work.';
    } finally {
      busy = false;
    }
  }

  // Leaving ends the viewer's access, so there is no listing to reload.
  async function leave() {
    busy = true;
    error = '';
    try {
      await removeRoomMember(roomId, userId!);
      onLeft?.();
    } catch (e) {
      error = e instanceof Error ? e.message : 'That didn’t work.';
    } finally {
      busy = false;
    }
  }

  function add() {
    const target = pick;
    confirmAdd = false;
    pick = '';
    if (target) run(() => addRoomMember(roomId, target));
  }
</script>

<div class="members">
  {#if data}
    <ul>
      {#each data.members as m (m.user_id)}
        <li>
          <span class="name">{m.display_name}</span>
          {#if m.is_owner}<span class="caption">created the room</span>{/if}
          {#if !talkBound && !m.is_owner && (data.can_manage || m.user_id === userId)}
            <button
              class="remove"
              type="button"
              disabled={busy}
              onclick={() =>
                m.user_id === userId ? leave() : run(() => removeRoomMember(roomId, m.user_id))}
            >
              {m.user_id === userId ? 'Leave' : 'Remove'}
            </button>
          {/if}
        </li>
      {/each}
    </ul>
    {#if talkBound}
      <p class="caption">Membership of a room on Nextcloud Talk is changed in Talk.</p>
    {:else if data.can_manage}
      <div class="add-row">
        <Select
          value={pick}
          options={candidates}
          onValueChange={(v) => (pick = v)}
          ariaLabel="Member to add"
          fullWidth
          disabled={busy}
        />
        <Button size="sm" disabled={!pick || busy} onclick={() => (confirmAdd = true)}>Add</Button>
      </div>
    {/if}
  {:else if !error}
    <p class="caption">Loading…</p>
  {/if}
  {#if error}<p class="form-error">{error}</p>{/if}
</div>

<ConfirmDialog
  bind:open={confirmAdd}
  title={`Add ${pickedName}`}
  message={disclosure}
  confirmLabel="Add and share the history"
  confirmVariant="primary"
  onConfirm={add}
/>

<style>
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1) 0;
    font-size: var(--text-sm);
  }
  .name {
    color: var(--text-primary);
  }
  .remove {
    margin-left: auto;
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    font-size: var(--text-xs);
    color: var(--text-dim);
    cursor: pointer;
  }
  .remove:hover:not(:disabled) {
    color: var(--status-danger-fg);
  }
  .add-row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    margin-top: var(--space-2);
  }
  p {
    margin: var(--space-1) 0 0;
  }
</style>
