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

/** The server's way-back sentence split on backtick spans, so the command it
 * names renders as code rather than with its backticks showing. */
export function codeSpans(text: string): { text: string; code: boolean }[] {
  return text
    .split(/(`[^`]+`)/)
    .filter((part) => part !== '')
    .map((part) =>
      part.length > 2 && part.startsWith('`') && part.endsWith('`')
        ? { text: part.slice(1, -1), code: true }
        : { text: part, code: false },
    );
}
