import type { WorkspaceAccess, WorkspacePlace } from '$lib/api';

export const ACCESS_LABELS: Record<WorkspaceAccess, string> = {
  rw: 'Read-write',
  ro: 'Read-only',
  skill: 'Via skill',
  none: 'No access',
  conditional: 'Depends on room',
};

// Each follows the bot's name directly, so the possessive takes no space.
const TASK_REACH_DESCRIPTIONS: Record<WorkspaceAccess, string | null> = {
  rw: ' can read and change these files',
  ro: ' can read these files',
  skill: ' reaches this through a skill',
  conditional: '’s access depends on the room',
  none: null,
};

export type AccessVariant = 'success' | 'info' | 'warn' | 'danger' | 'neutral';

// A ramp from most to least reach; an access kind this client does not know stays neutral.
const ACCESS_VARIANTS: Record<WorkspaceAccess, AccessVariant> = {
  rw: 'success',
  ro: 'info',
  skill: 'info',
  conditional: 'warn',
  none: 'danger',
};

export function accessVariant(access: WorkspaceAccess): AccessVariant {
  return Object.hasOwn(ACCESS_VARIANTS, access) ? ACCESS_VARIANTS[access] : 'neutral';
}

export function accessLabel(access: WorkspaceAccess): string {
  return Object.hasOwn(ACCESS_LABELS, access) ? ACCESS_LABELS[access] : access;
}

export function reachesTasks(access: WorkspaceAccess): boolean {
  return Object.hasOwn(TASK_REACH_DESCRIPTIONS, access) && TASK_REACH_DESCRIPTIONS[access] !== null;
}

// An access kind this client does not know gets no description, so no icon claims reach it may not have.
// Where a folder reaches less than every room, its description says so: a room folder is bound
// only for that room's own tasks, a group folder in a shared room only when every reader belongs.
export type TaskReachScope = 'room' | 'rooms' | 'group';

const SCOPE_SUFFIXES: Record<TaskReachScope, string> = {
  room: 'in this room only',
  rooms: 'each only in its own room',
  group: 'in a shared room only when everyone there is in the group',
};

export function taskReachDescription(
  access: WorkspaceAccess,
  botName: string,
  scope?: TaskReachScope,
): string | null {
  if (!reachesTasks(access)) return null;
  const description = `${botName}${TASK_REACH_DESCRIPTIONS[access]}`;
  return scope ? `${description}, ${SCOPE_SUFFIXES[scope]}` : description;
}

// The index caption's version of the icon, for a list whose every entry shares one reach.
// Only read and read-write are spelled out; any other access leaves the caption without a note.
export function taskReachNote(
  access: WorkspaceAccess,
  botName: string,
  scope?: TaskReachScope,
): string | null {
  const verb = access === 'rw' ? 'can read and change' : access === 'ro' ? 'can read' : null;
  if (!verb) return null;
  return `${botName} ${verb} these files, ${scope ? SCOPE_SUFFIXES[scope] : 'in any of your rooms'}.`;
}

// An index's places share one icon only when they share one access; a mixed set claims none.
export function sharedAccess(places: WorkspacePlace[]): WorkspaceAccess | null {
  const first = places[0]?.reach.private;
  if (!first) return null;
  return places.every((place) => place.reach.private === first) ? first : null;
}

export function placeForRef(places: WorkspacePlace[], ref: string): WorkspacePlace | null {
  let best: WorkspacePlace | null = null;
  for (const place of places) {
    const root = place.view_root;
    if (!place.browsable || !root) continue;
    if (ref !== root && !ref.startsWith(root + '/')) continue;
    if (!best || root.length > best.view_root!.length) best = place;
  }
  return best;
}
