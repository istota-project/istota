import { base } from '$app/paths';

/** How each delivery channel is named everywhere in briefing settings. */
export const CHANNEL_LABELS: Record<string, string> = {
  in_app: 'In app',
  email: 'Email',
  talk: 'Talk',
  ntfy: 'ntfy',
  web: 'Web',
};

export function channelLabel(channel: string): string {
  return CHANNEL_LABELS[channel] ?? channel;
}

/** How each source kind is named in the block editor and the shared-block editor. */
export const SOURCE_KIND_LABELS: Record<string, string> = {
  rss: 'RSS feed',
  email: 'Newsletters',
  my_email: 'My email',
  browse: 'Web page',
  markets: 'Markets',
  calendar: 'Calendar',
  todos: 'Todos',
  reminders: 'Reminders',
  notes: 'Notes',
  shared_block: 'Shared block',
  group_memory: 'Group notebook',
  group_file: 'Group file',
  personal_file: "A member's file",
  personal_calendar: "A member's calendar",
};

/** The source kinds a group briefing can run; `briefings.models.GROUP_SOURCE_KINDS` on the server. */
export const GROUP_SOURCE_KINDS = [
  'browse',
  'markets',
  'email',
  'group_memory',
  'group_file',
  'shared_block',
  'personal_file',
  'personal_calendar',
];

export function sourceKindLabel(kind: string): string {
  return SOURCE_KIND_LABELS[kind] ?? kind;
}

/** Briefing settings are addressed by URL, so the sidebar and every editor agree. */
export type BriefingSettingsTarget =
  | { kind: 'personal'; name: string }
  | { kind: 'group'; group: string; name: string }
  | { kind: 'new'; group?: string }
  | { kind: 'view'; view: 'delivery' | 'shared' };

export function briefingSettingsUrl(target: BriefingSettingsTarget): string {
  const query = new URLSearchParams();
  if (target.kind === 'personal') query.set('name', target.name);
  else if (target.kind === 'group') {
    query.set('group', target.group);
    query.set('briefing', target.name);
  } else if (target.kind === 'new') {
    query.set('new', '1');
    if (target.group) query.set('group', target.group);
  } else query.set('view', target.view);
  return `${base}/briefings/settings/?${query}`;
}

/** Preview of the title a blank `title` produces.
 *
 * Display hint only: `briefings.generate.default_briefing_title` is the
 * authoritative rule, and the two are kept in step by hand. The run date the
 * renderer appends is not shown.
 */
export function derivedTitle(name: string): string {
  const words = name
    .trim()
    .split(/[-_\s]+/)
    .filter(Boolean);
  if (!words.length) return 'Briefing';
  const label = words.map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');
  return label.toLowerCase().includes('brief') ? label : `${label} Briefing`;
}

/** A briefing name from a title: lowercase letters, digits, `-` and `_`. */
export function slugify(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^[-_]+|[-_]+$/g, '')
    .slice(0, 90);
}

/** A name that does not collide: briefing saves are upserts keyed on name. */
export function uniqueName(stem: string, taken: string[]): string {
  const used = new Set(taken);
  if (!used.has(`${stem}-copy`)) return `${stem}-copy`;
  for (let n = 2; ; n += 1) {
    const candidate = `${stem}-copy-${n}`;
    if (!used.has(candidate)) return candidate;
  }
}

type Contribution = { kind: string; config: Record<string, unknown> };

/** What a calendar contribution discloses, in words: fields and the date window. */
export function calendarDisclosure(config: Record<string, unknown>): string {
  const fields = (config.fields as string[] | undefined) ?? ['time', 'title'];
  const back = Number(config.lookback_days ?? 0);
  const ahead = Number(config.lookahead_days ?? 1);
  const window =
    back === 0 && ahead === 0
      ? 'today'
      : back === 0
        ? `today and the next ${ahead === 1 ? 'day' : `${ahead} days`}`
        : `the last ${back === 1 ? 'day' : `${back} days`} to the next ${ahead === 1 ? 'day' : `${ahead} days`}`;
  const privateNote = config.include_private === true ? ', private events included' : '';
  return `${fields.join(', ')} for ${window}${privateNote}`;
}

/** One line naming a contributed resource without a raw URL where a label exists. */
export function contributionLabel(
  source: Contribution,
  calendars: { value: string; label: string }[] = [],
): string {
  if (source.kind === 'personal_file') return String(source.config.path ?? 'No file chosen');
  const url = String(source.config.calendar_url ?? '');
  if (!url) return 'No calendar chosen';
  const known = calendars.find((c) => c.value === url);
  return known?.label ?? decodeURIComponent(url.replace(/\/+$/, '').split('/').pop() ?? url);
}
