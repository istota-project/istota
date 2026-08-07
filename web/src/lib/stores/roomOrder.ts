import type { ChatRoom } from '$lib/api';

/**
 * Sidebar room order: most recently active first.
 *
 * The server sends the list in this order already (`db.list_member_rooms`), so
 * this exists for what happens after — a message streaming into a background
 * room has to move it without a refetch, and the 30s rooms poll merges in place
 * rather than adopting the fresh order.
 *
 * `last_activity` is an ISO-UTC string in the same format a message row's
 * `created_at` carries, so it sorts lexicographically and a streamed stamp can
 * be written straight onto the room.
 */

/** Descending by `last_activity`, in a new array. Stable, so rooms sharing a
 * stamp — and rooms carrying none at all, which an older backend produces —
 * keep the relative order they arrived in rather than being reversed by a
 * missing key. */
export function sortRoomsByActivity(rooms: ChatRoom[]): ChatRoom[] {
  return rooms.slice().sort((a, b) => (b.last_activity ?? '').localeCompare(a.last_activity ?? ''));
}

/** Advance one room's activity stamp and re-sort.
 *
 * Returns the input array unchanged when nothing moved — an unknown token, no
 * stamp, or a stamp that isn't newer. That last case is the load-bearing one:
 * gap recovery and the reconnect replay both redeliver older rows, and a stamp
 * allowed to go backwards would demote the room the user just spoke in.
 */
export function touchRoomActivity(
  rooms: ChatRoom[],
  token: string,
  at: string | undefined,
): ChatRoom[] {
  if (!at) return rooms;
  const idx = rooms.findIndex((r) => r.token === token);
  if (idx === -1) return rooms;
  if ((rooms[idx].last_activity ?? '') >= at) return rooms;
  const next = rooms.slice();
  next[idx] = { ...next[idx], last_activity: at };
  return sortRoomsByActivity(next);
}
