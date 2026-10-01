import type { RoomOffView } from './api';

/** Who switched a room off, as one sentence for the chat page's notice. */
export function describeRoomOff(off: RoomOffView): string {
  if (off.by.length === 0) {
    return 'It was switched off when it was removed from the group.';
  }
  const names = off.by.map((v) => {
    const notes = [v.guest ? 'a guest' : '', v.agreed ? 'has agreed to switch it back on' : '']
      .filter(Boolean)
      .join(', ');
    return notes ? `${v.name} (${notes})` : v.name;
  });
  const list =
    names.length === 1
      ? names[0]
      : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
  return `Switched off by ${list}.`;
}
