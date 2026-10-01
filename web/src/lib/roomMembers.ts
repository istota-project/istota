/**
 * A room's member list, fetched per room and kept for a minute.
 *
 * Two readers: the transcript, which styles `@name` mentions of members
 * (ISSUE-578), and the composer's `@` autocomplete (ISSUE-580). Both want the
 * same list for the active room, and neither should fetch it per message or
 * per keystroke.
 *
 * Membership changes in more ways than this client sees: another member's
 * add or remove, a member leaving, a Talk room's roster, an istota user's
 * first turn. None of them reaches the page as an event, so an entry lives
 * `MEMBERS_TTL_MS` and the page asks again whenever the room list updates,
 * which bounds how stale a list can get. A failed fetch is cached as empty
 * for the same lifetime, which is also its retry interval. `dropRoomMembers`
 * is for the change this viewer made, which should show at once.
 */
import { getRoomMembers, type RoomMember } from '$lib/api';

export const MEMBERS_TTL_MS = 60_000;

const cache = new Map<number, { at: number; members: Promise<RoomMember[]> }>();

export function loadRoomMembers(roomId: number, now: number = Date.now()): Promise<RoomMember[]> {
  const hit = cache.get(roomId);
  if (hit && now - hit.at < MEMBERS_TTL_MS) return hit.members;
  const members = getRoomMembers(roomId).then(
    (r) => r.members,
    (e) => {
      console.warn('room member fetch failed', e);
      return [];
    },
  );
  cache.set(roomId, { at: now, members });
  return members;
}

export function dropRoomMembers(roomId: number): void {
  cache.delete(roomId);
}
