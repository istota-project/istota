<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { MessageSquare } from '@lucide/svelte';
  import {
    Icon,
    Badge,
    Button,
    CountPill,
    EmptyState,
    Card,
    Field,
    FormStack,
    Input,
    Timestamp,
  } from '@minot/ui';
  import { createGroupRoom, getChatRooms, type ChatRoom, type GroupDetail } from '$lib/api';

  let {
    rooms,
    groupId,
    revision,
    canCreate = false,
    onChanged,
  }: {
    rooms: GroupDetail['rooms'];
    groupId: string;
    revision: number;
    /** Only a current owner creates a room, and the server checks again. */
    canCreate?: boolean;
    /** A room was created: the group's detail and revision moved. */
    onChanged?: () => void | Promise<void>;
  } = $props();
  let chatRooms = $state<Map<string, ChatRoom>>(new Map());
  let active = true;

  const ordered = $derived(
    rooms
      .map((room) => ({ ...room, chat: chatRooms.get(room.token) }))
      .sort((a, b) => (b.chat?.last_activity ?? '').localeCompare(a.chat?.last_activity ?? '')),
  );

  // A group with no room of its own yet is offered the usual first one. The
  // card is remounted with each group, so the first value is the one wanted.
  let name = $state(
    untrack(() => (rooms.some((room) => room.membership_source === 'group') ? '' : 'General')),
  );
  let creating = $state(false);
  let error = $state('');
  // The idempotency token, kept across a failed attempt so a retry of the same
  // name returns the room the first attempt may have made. A new name gets a
  // new token, since the server refuses one token for two names.
  let requestId = '';
  let requestName = '';

  function newRequestId(): string {
    if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
    return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  async function create() {
    const trimmed = name.trim();
    if (!trimmed || creating) return;
    if (!requestId || requestName !== trimmed) {
      requestId = newRequestId();
      requestName = trimmed;
    }
    creating = true;
    error = '';
    try {
      await createGroupRoom(groupId, { name: trimmed, request_id: requestId, revision });
      requestId = '';
      name = '';
      await onChanged?.();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Could not create the room.';
    } finally {
      creating = false;
    }
  }

  onMount(() => {
    // Activity is decoration on a list the group detail already returned, so a
    // failed room listing leaves the names in their original order.
    if (rooms.length)
      getChatRooms()
        .then((result) => {
          if (active) chatRooms = new Map(result.rooms.map((room) => [room.token, room]));
        })
        .catch(() => {});
    return () => {
      active = false;
    };
  });
</script>

<Card title="Rooms" description="Chat rooms for this group you can open, most recent first.">
  {#if ordered.length}
    <ul class="rooms list-reset">
      {#each ordered as room (room.token)}
        <li>
          <span class="entry-icon" aria-hidden="true"><Icon icon={MessageSquare} size="md" /></span>
          <a class="name" href="{base}/chat?room={encodeURIComponent(room.token)}" title={room.name}
            >{room.name}</a
          >
          {#if room.membership_source === 'group'}
            <Badge size="sm" tone="subtle" uppercase={false}>Group room</Badge>
          {:else if room.membership_source === 'room'}
            <Badge size="sm" tone="subtle" uppercase={false}>Linked</Badge>
          {/if}
          <Timestamp class="caption" value={room.chat?.last_activity} format="relative" />
          <CountPill count={room.chat?.unread_count ?? 0} />
        </li>
      {/each}
    </ul>
    <p class="caption">
      A group room holds everyone in the group. A linked room keeps its own members.
    </p>
  {:else}<EmptyState size="sm">No rooms for this group you can access.</EmptyState>{/if}
  {#if canCreate}
    <FormStack as="form" onsubmit={create} disabled={creating}>
      <Field
        label="Room name"
        description="Everyone in the group joins it, and can read its whole history."
        error={error || undefined}
      >
        <Input bind:value={name} maxlength={200} />
      </Field>
      <div class="form-actions">
        <Button
          type="submit"
          size="sm"
          disabled={!name.trim()}
          loading={creating}
          loadingLabel="Creating…">Create room</Button
        >
      </div>
    </FormStack>
  {/if}
</Card>

<style>
  .rooms li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
  .rooms li:last-child {
    border-bottom: none;
  }
  /* The Files table's name cell: an amber icon, the name in the body colour. */
  .entry-icon {
    display: flex;
    flex-shrink: 0;
    color: var(--accent-amber);
  }
  .name {
    flex: 1;
    min-width: 0;
    color: var(--text-primary);
    text-decoration: none;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .name:hover {
    text-decoration: underline;
  }
</style>
