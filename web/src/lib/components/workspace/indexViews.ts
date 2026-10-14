/** Views that open an index in the main area rather than a folder. */
export type IndexView = 'rooms' | 'groups' | 'config' | 'skills' | 'scripts' | 'prompts';

export const INDEX_VIEWS: readonly IndexView[] = [
  'rooms',
  'groups',
  'config',
  'skills',
  'scripts',
  'prompts',
];

export const INDEX_VIEW_LABELS: Record<IndexView, string> = {
  rooms: 'Rooms',
  groups: 'Groups',
  config: 'Configuration',
  skills: 'Skill instructions',
  scripts: 'Scripts',
  prompts: 'Prompt files',
};

export function parseIndexView(value: string | null | undefined): IndexView | null {
  return INDEX_VIEWS.find((view) => view === value) ?? null;
}

/** The index a room or group folder belongs to, for its breadcrumb and the sidebar's active entry. */
export function indexViewForKind(kind: string | undefined): IndexView | null {
  if (kind === 'room') return 'rooms';
  if (kind === 'group') return 'groups';
  return null;
}
