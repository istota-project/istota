<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { MessageSquare } from '@lucide/svelte';
  import { CountPill, EmptyState, Card, Timestamp } from '$lib/components/ui';
  import { getChatRooms, type ChatRoom, type GroupDetail } from '$lib/api';

  let { rooms }: { rooms: GroupDetail['rooms'] } = $props();
  let chatRooms = $state<Map<string, ChatRoom>>(new Map());
  let active = true;

  const ordered = $derived(
    rooms
      .map((room) => ({ ...room, chat: chatRooms.get(room.token) }))
      .sort((a, b) => (b.chat?.last_activity ?? '').localeCompare(a.chat?.last_activity ?? '')),
  );

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

<Card title="Rooms" description="Linked chat rooms you can open, most recent first.">
  {#if ordered.length}
    <ul class="rooms list-reset">
      {#each ordered as room (room.token)}
        <li>
          <span class="entry-icon" aria-hidden="true"><MessageSquare size={16} /></span>
          <a class="name" href="{base}/chat?room={encodeURIComponent(room.token)}" title={room.name}
            >{room.name}</a
          >
          <Timestamp class="caption" value={room.chat?.last_activity} format="relative" />
          <CountPill count={room.chat?.unread_count ?? 0} />
        </li>
      {/each}
    </ul>
  {:else}<EmptyState size="sm">No linked rooms you can access.</EmptyState>{/if}
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
