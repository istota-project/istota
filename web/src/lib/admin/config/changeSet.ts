/**
 * The config editor's pending change set, as plain data.
 *
 * The page holds one `ConfigDraft` and replaces it on every edit; these
 * functions are the only way it changes, so the count in the footer and the
 * change set sent to preview and submit are read off one value. A field is
 * addressed by its `path`, never its dotted `key`: a segment may hold a dot.
 */
import type {
  AdminConfigField,
  AdminConfigView,
  ConfigChangeSet,
  ConfigEditorKind,
  ConfigOp,
} from '$lib/api';

export interface ConfigDraft {
  /** One op per path, keyed by `pathKey`. */
  ops: Record<string, ConfigOp>;
  /** A credential name (a secrets file, or an inline field's label) to its
   *  new value; `""` clears it. */
  secrets: Record<string, string>;
  /** The whole new admin list, or null for unchanged. */
  admins: string[] | null;
}

export function emptyDraft(): ConfigDraft {
  return { ops: {}, secrets: {}, admins: null };
}

export function pathKey(path: string[]): string {
  return JSON.stringify(path);
}

/** `draft` with `op` at its path, or with the path's op removed when `op` is
 *  null (then `path` names it). */
export function withOp(draft: ConfigDraft, op: ConfigOp | null, path?: string[]): ConfigDraft {
  const ops = { ...draft.ops };
  const key = pathKey(op ? op.path : (path ?? []));
  if (op) ops[key] = op;
  else delete ops[key];
  return { ...draft, ops };
}

/** `draft` with a credential's new value, or without it when `value` is null. */
export function withSecret(draft: ConfigDraft, name: string, value: string | null): ConfigDraft {
  const secrets = { ...draft.secrets };
  if (value === null) delete secrets[name];
  else secrets[name] = value;
  return { ...draft, secrets };
}

export function withAdmins(draft: ConfigDraft, admins: string[] | null): ConfigDraft {
  return { ...draft, admins: admins ? [...admins] : null };
}

export function changeCount(draft: ConfigDraft): number {
  return Object.keys(draft.ops).length + Object.keys(draft.secrets).length + (draft.admins ? 1 : 0);
}

export function buildChangeSet(draft: ConfigDraft, baseDigest: string): ConfigChangeSet {
  const changeSet: ConfigChangeSet = {
    base_digest: baseDigest,
    ops: Object.values(draft.ops),
    raw: null,
    secrets: { ...draft.secrets },
  };
  if (draft.admins) changeSet.admins = [...draft.admins];
  return changeSet;
}

/** Whether two field values are the same setting. An empty string and an
 *  absent value are: an optional string field shows as an empty input. */
export function sameValue(a: unknown, b: unknown): boolean {
  const norm = (v: unknown) => (v === null || v === undefined ? '' : v);
  return JSON.stringify(norm(a)) === JSON.stringify(norm(b));
}

/** The op for setting `field` to `value`, or null when that is what it holds. */
export function opFor(field: AdminConfigField, value: unknown): ConfigOp | null {
  if (sameValue(value, field.value)) return null;
  return { op: 'set', path: field.path, value };
}

/** What a control shows: the pending value, the default after a reset, or
 *  the value the running process loaded. */
export function displayValue(field: AdminConfigField, op: ConfigOp | undefined): unknown {
  if (op?.op === 'set') return op.value;
  if (op?.op === 'unset') return field.default;
  return field.value;
}

/** A value as text, for a read-only field or a default. `—` for nothing. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (Array.isArray(value)) return value.length ? value.map((x) => String(x)).join(', ') : '—';
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return '—';
    return entries.map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join('\n');
  }
  return String(value);
}

export function parseNumber(text: string, kind: 'int' | 'float'): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  if (!Number.isFinite(n)) return null;
  if (kind === 'int' && !Number.isInteger(n)) return null;
  return n;
}

/** A list typed one item per line; blank lines are dropped. Null when the
 *  list holds numbers and a line is not one. */
export function parseList(text: string, numeric: boolean): unknown[] | null {
  const items = text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '');
  if (!numeric) return items;
  const numbers = items.map(Number);
  return numbers.every(Number.isFinite) ? numbers : null;
}

const EDITABLE: ReadonlySet<ConfigEditorKind> = new Set([
  'bool',
  'int',
  'float',
  'str',
  'path',
  'list',
  'choice',
  'multi_choice',
  'secret',
]);

/**
 * `draft` reduced to what `view` still lets the admin change.
 *
 * After `config_changed` the page reloads the view and keeps the local change
 * set where it still applies: an op whose field is gone or became read-only,
 * and a credential no editable field or secrets file names, are dropped.
 * `dropped` counts them so the page can say so.
 */
export function retainExisting(
  draft: ConfigDraft,
  view: AdminConfigView,
): { draft: ConfigDraft; dropped: number } {
  const editablePaths = new Set<string>();
  const credentials = new Set<string>();
  for (const section of view.sections) {
    for (const field of section.fields) {
      if (!EDITABLE.has(field.editor)) continue;
      if (field.editor === 'secret' && field.credential) credentials.add(field.credential.name);
      else editablePaths.add(pathKey(field.path));
    }
  }
  for (const file of view.secrets) {
    if (!file.read_only_reason) credentials.add(file.name);
  }

  let dropped = 0;
  const kept = emptyDraft();
  for (const [key, op] of Object.entries(draft.ops)) {
    if (editablePaths.has(key)) kept.ops[key] = op;
    else dropped += 1;
  }
  for (const [name, value] of Object.entries(draft.secrets)) {
    if (credentials.has(name)) kept.secrets[name] = value;
    else dropped += 1;
  }
  if (draft.admins) {
    if (view.admins.read_only_reason) dropped += 1;
    else kept.admins = [...draft.admins];
  }
  return { draft: kept, dropped };
}
