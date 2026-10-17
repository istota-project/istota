/**
 * The servarr card's edit form, as plain data.
 *
 * The card edits `[servarr]` only by building a config-editor change set
 * (`$lib/admin/config/changeSet`) and handing it to the shared review, step-up
 * and request flow, as the Configuration page does. Plain fields become `set`
 * and `unset` ops by path. An API key is a file credential, so a new one goes
 * in the change set's `secrets` under its secrets file's name, the name the
 * view's `secrets` entry carries, exactly as the Configuration page sends one:
 * never in an op, and the host applier writes the file. A stored key never
 * reaches the browser: the form only knows whether one is set.
 */
import type {
  AdminConfigView,
  AdminServarrConnection,
  ConfigOp,
  ConfigSecretFile,
  ServarrApp,
} from '$lib/api';
import {
  emptyDraft,
  parseList,
  sameValue,
  withOp,
  withSecret,
  type ConfigDraft,
} from '$lib/admin/config/changeSet';

/** The known apps, in the order the skill visits them (`SERVARR_APPS`). */
export const SERVARR_APPS: ServarrApp[] = ['sonarr', 'radarr', 'lidarr', 'prowlarr'];

/** The add defaults each app takes (`SERVARR_DEFAULT_FIELDS` in config.py). */
export const DEFAULT_FIELDS: Record<ServarrApp, string[]> = {
  sonarr: ['quality_profile', 'root_folder', 'monitor'],
  radarr: ['quality_profile', 'root_folder', 'minimum_availability'],
  lidarr: ['quality_profile', 'root_folder', 'metadata_profile', 'monitor'],
  prowlarr: [],
};

export interface ServarrAppForm {
  /** Whether the app has a `[servarr.apps.<app>]` table. */
  enabled: boolean;
  url: string;
  /** A new key typed in this session; `""` keeps the stored one. */
  key: string;
  /** Whether a key is stored now; null for a table the loader dropped. */
  keySet: boolean | null;
  /** The stored table is missing its url or key, so neither is known. */
  incomplete: boolean;
  /** The key's secrets file, from the view's `secrets` entry; null when the
   *  view lists none. */
  keyFile: string | null;
  /** Why the key cannot be changed here (that entry's `read_only_reason`), or null. */
  keyReadOnly: string | null;
}

export interface ServarrForm {
  apps: Record<ServarrApp, ServarrAppForm>;
  requesters: string[];
  /** `[servarr] private_hosts`, one per line as typed. */
  privateHosts: string;
  defaults: Record<ServarrApp, Record<string, string>>;
}

const BASE = ['servarr'];

const NO_KEY_FILE = 'This install has no secrets file for this key, so it is set on the host.';

/** The view's `secrets` entry for an app's key, matched by its config key. */
function keyEntry(secrets: ConfigSecretFile[], app: ServarrApp): ConfigSecretFile | undefined {
  return secrets.find((entry) => entry.config_key === `servarr.apps.${app}.api_key`);
}

export function servarrForm(
  connection: AdminServarrConnection,
  privateHosts: string[],
  secrets: ConfigSecretFile[],
): ServarrForm {
  const apps = {} as Record<ServarrApp, ServarrAppForm>;
  const defaults = {} as Record<ServarrApp, Record<string, string>>;
  for (const name of SERVARR_APPS) {
    const stored = connection.apps.find((entry) => entry.app === name);
    const file = keyEntry(secrets, name);
    apps[name] = {
      enabled: !!stored,
      url: stored?.url ?? '',
      key: '',
      keySet: stored ? stored.api_key_set : false,
      incomplete: stored?.incomplete ?? false,
      keyFile: file?.name ?? null,
      keyReadOnly: file ? file.read_only_reason : NO_KEY_FILE,
    };
    const configured = connection.defaults[name] ?? {};
    defaults[name] = Object.fromEntries(
      DEFAULT_FIELDS[name].map((field) => [field, configured[field] ?? '']),
    );
  }
  return {
    apps,
    requesters: [...connection.requesters],
    privateHosts: privateHosts.join('\n'),
    defaults,
  };
}

/** `[servarr] private_hosts` as the Configuration view loaded it. */
export function viewPrivateHosts(view: AdminConfigView): string[] {
  const section = view.sections.find((item) => item.key === 'servarr');
  const field = section?.fields.find((item) => item.name === 'private_hosts');
  return Array.isArray(field?.value) ? field.value.map((item) => String(item)) : [];
}

/** The fields the card writes; a reason on any of them makes it read-only. */
const CARD_FIELDS = new Set(['apps', 'requesters', 'private_hosts', 'defaults']);

/**
 * Why the card cannot change `[servarr]`, or null when it can.
 *
 * Ownership is per resource, not per section (`applier.read_managed`), so
 * `[servarr]` is managed exactly when the whole config is.
 */
export function servarrReadOnlyReason(view: AdminConfigView): string | null {
  if (!view.applier.available) return view.applier.reason;
  if (!view.editable) {
    return 'This process has no config.toml to change, so the connection is changed on the host.';
  }
  const managed = view.managed.config;
  if (managed.managed) {
    if (managed.source === 'unreadable') {
      return (
        'config/managed.json cannot be read, so all of config.toml reads as managed. ' +
        'Re-run istota-stack apply on the host with the intended [meta] to repair it.'
      );
    }
    return (
      `config.toml is managed by ${managed.source ?? 'a plan'}, so [servarr] is changed in its ` +
      'settings plan, with the API keys in its secrets files. Its next run would revert a ' +
      'change made here.'
    );
  }
  const section = view.sections.find((item) => item.key === 'servarr');
  for (const field of section?.fields ?? []) {
    if (CARD_FIELDS.has(field.name) && field.read_only_reason) return field.read_only_reason;
  }
  return null;
}

function hostList(text: string): string[] {
  return parseList(text, false) as string[];
}

function normalHost(host: string): string {
  return host
    .toLowerCase()
    .replace(/^\[|\]$/g, '')
    .replace(/\.$/, '');
}

/** What is wrong with the URL, mirroring the skill's checks before a request. */
function urlProblem(url: string, privateHosts: string[]): string | null {
  const text = url.trim();
  if (!text) return 'Enter the URL of the app.';
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    return 'Not a URL: it needs http:// or https:// and a host.';
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return 'The URL must start with http:// or https://.';
  }
  if (!parsed.hostname) return 'The URL has no host.';
  if (!/^https?:\/\//i.test(text)) return 'The URL must start with http:// or https://.';
  if (parsed.username || parsed.password) {
    return 'The URL must not carry a user name or password; the API key is set below.';
  }
  if (parsed.search || parsed.hash || text.includes('?') || text.includes('#')) {
    return 'The URL must not carry a query or fragment.';
  }
  if (parsed.protocol === 'http:') {
    const allowed = new Set(privateHosts.map(normalHost));
    if (!allowed.has(normalHost(parsed.hostname))) {
      return (
        `Plain HTTP goes only to a host in the private hosts, and ${parsed.hostname} is not ` +
        'in the private hosts. Add it there or use https://.'
      );
    }
  }
  return null;
}

/** Problems keyed by `<app>.url` or `<app>.key`; empty when the form can be reviewed. */
export function formProblems(form: ServarrForm): Record<string, string> {
  const problems: Record<string, string> = {};
  const privateHosts = hostList(form.privateHosts);
  for (const name of SERVARR_APPS) {
    const app = form.apps[name];
    if (!app.enabled) continue;
    const problem = urlProblem(app.url, privateHosts);
    if (problem) problems[`${name}.url`] = problem;
    if (app.keySet !== true && app.key === '') {
      problems[`${name}.key`] = app.keyReadOnly
        ? `The API key is not set and cannot be set here. ${app.keyReadOnly}`
        : app.incomplete
          ? 'The stored table is incomplete; enter the API key again.'
          : 'Enter the API key of the app.';
    }
  }
  return problems;
}

/** The change `form` makes to what `initial` was built from. */
export function servarrDraft(form: ServarrForm, initial: ServarrForm): ConfigDraft {
  let draft = emptyDraft();
  const op = (next: ConfigOp) => {
    draft = withOp(draft, next);
  };

  for (const name of SERVARR_APPS) {
    const app = form.apps[name];
    const before = initial.apps[name];
    const path = [...BASE, 'apps', name];
    const keyFile = app.keyReadOnly ? null : app.keyFile;
    if (!app.enabled) {
      if (before.enabled) {
        op({ op: 'unset', path });
        // The key outlives its table in a secrets file; clear it with the table.
        if (keyFile) draft = withSecret(draft, keyFile, '');
      }
      continue;
    }
    const url = app.url.trim();
    if (!before.enabled || before.incomplete || url !== before.url) {
      op({ op: 'set', path: [...path, 'url'], value: url });
    }
    if (keyFile && app.key !== '') draft = withSecret(draft, keyFile, app.key);
  }

  if (!sameValue([...form.requesters].sort(), [...initial.requesters].sort())) {
    op(
      form.requesters.length
        ? { op: 'set', path: [...BASE, 'requesters'], value: [...form.requesters] }
        : { op: 'unset', path: [...BASE, 'requesters'] },
    );
  }

  const hosts = hostList(form.privateHosts);
  if (!sameValue(hosts, hostList(initial.privateHosts))) {
    op(
      hosts.length
        ? { op: 'set', path: [...BASE, 'private_hosts'], value: hosts }
        : { op: 'unset', path: [...BASE, 'private_hosts'] },
    );
  }

  for (const name of SERVARR_APPS) {
    for (const field of DEFAULT_FIELDS[name]) {
      const value = (form.defaults[name][field] ?? '').trim();
      if (value === (initial.defaults[name][field] ?? '')) continue;
      const path = [...BASE, 'defaults', name, field];
      op(value ? { op: 'set', path, value } : { op: 'unset', path });
    }
  }
  return draft;
}

function pick<T>(edited: T, before: T, after: T): T {
  return sameValue(edited, before) ? after : edited;
}

/**
 * `form` moved onto `after`, the values of a config.toml that changed under
 * the card: what the admin changed from `before` is kept, the rest is read
 * from the new file, so a review does not revert somebody else's change.
 */
export function rebaseForm(
  form: ServarrForm,
  before: ServarrForm,
  after: ServarrForm,
): ServarrForm {
  const apps = {} as Record<ServarrApp, ServarrAppForm>;
  const defaults = {} as Record<ServarrApp, Record<string, string>>;
  for (const name of SERVARR_APPS) {
    const edited = form.apps[name];
    const was = before.apps[name];
    const now = after.apps[name];
    apps[name] = {
      enabled: pick(edited.enabled, was.enabled, now.enabled),
      url: pick(edited.url, was.url, now.url),
      key: edited.key,
      keySet: now.keySet,
      incomplete: now.incomplete,
      keyFile: now.keyFile,
      keyReadOnly: now.keyReadOnly,
    };
    defaults[name] = Object.fromEntries(
      DEFAULT_FIELDS[name].map((field) => [
        field,
        pick(form.defaults[name][field], before.defaults[name][field], after.defaults[name][field]),
      ]),
    );
  }
  return {
    apps,
    requesters: pick(form.requesters, before.requesters, after.requesters),
    privateHosts: pick(form.privateHosts, before.privateHosts, after.privateHosts),
    defaults,
  };
}
