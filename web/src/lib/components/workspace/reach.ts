import type { WorkspaceAccess, WorkspacePlace } from '$lib/api';

export const ACCESS_LABELS: Record<WorkspaceAccess, string> = {
  rw: 'Read-write',
  ro: 'Read-only',
  skill: 'Via skill',
  none: 'No access',
  conditional: 'Depends on room',
};

const TASK_REACH_DESCRIPTIONS: Record<WorkspaceAccess, string | null> = {
  rw: 'Istota can read and change these files',
  ro: 'Istota can read these files',
  skill: 'Istota reaches this through a skill',
  conditional: 'Istota’s access depends on the room',
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
  return taskReachDescription(access) !== null;
}

// An access kind this client does not know gets no description, so no icon claims reach it may not have.
// A room folder is bound only for that room's own tasks, so its description says so.
export function taskReachDescription(access: WorkspaceAccess, room = false): string | null {
  if (!Object.hasOwn(TASK_REACH_DESCRIPTIONS, access)) return null;
  const description = TASK_REACH_DESCRIPTIONS[access];
  return description && room ? `${description}, in this room only` : description;
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
