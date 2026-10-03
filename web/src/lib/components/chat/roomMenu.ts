import type { ChatRoom } from '$lib/api';

export interface RoomMenuItem {
  label: string;
  onSelect: () => void;
}

export interface RoomMenuActions {
  settings: () => void;
  memory: () => void;
  notes: () => void;
}

/** A room's kebab entries. A shared room, or one the user already keeps notes
 * about, has two notes files: the room's own (`Room notes`, read by everyone in
 * it) and the user's private ones (`My notes`). Any other room has only the
 * first, under its old name. */
export function roomMenuItems(
  room: Pick<ChatRoom, 'shared' | 'has_my_notes'>,
  actions: RoomMenuActions,
): RoomMenuItem[] {
  const items: RoomMenuItem[] = [{ label: 'Settings', onSelect: actions.settings }];
  if (room.shared || room.has_my_notes) {
    items.push({ label: 'Room notes', onSelect: actions.memory });
    items.push({ label: 'My notes', onSelect: actions.notes });
  } else {
    items.push({ label: 'Memory', onSelect: actions.memory });
  }
  return items;
}
