<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { CountPill, EmptyState } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { getChatRooms, type ChatRoom, type GroupDetail } from '$lib/api';
  import { formatDateTime, formatRelative } from '$lib/dateFormat';

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

<SettingsCard title="Rooms" description="Linked chat rooms you can open, most recent first.">
  {#if ordered.length}
    <ul class="rooms prose">
      {#each ordered as room (room.token)}
        <li>
          <a href="{base}/chat?room={encodeURIComponent(room.token)}">{room.name}</a>
          {#if room.chat?.last_activity}<span
              class="caption"
              title={formatDateTime(room.chat.last_activity)}
              >{formatRelative(room.chat.last_activity)}</span
            >{/if}
          <CountPill count={room.chat?.unread_count ?? 0} />
        </li>
      {/each}
    </ul>
  {:else}<EmptyState size="sm">No linked rooms you can access.</EmptyState>{/if}
</SettingsCard>

<style>
  .rooms {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .rooms li {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
  .rooms li:last-child {
    border-bottom: none;
  }
  .rooms a {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
