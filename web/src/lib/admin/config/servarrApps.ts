/**
 * The Configuration page's `[servarr.apps]` editor, as plain data.
 *
 * One block per app: its URL is a `set` op on `servarr.apps.<app>.url`, and
 * its API key is a file credential, so a new one goes in the change set's
 * `secrets` under the file name the view's `secrets` entry carries, never in
 * an op; the host applier writes the file. A stored key never reaches the
 * browser: the form only knows whether one is set. The rest of `[servarr]`
 * (requesters, add defaults) is ordinary fields, and a grant for a private host
 * is an entry in the `[private_hosts]` table under General.
 */
import type {
  AdminConfigField,
  AdminServarrConnection,
  ConfigOp,
  ConfigSecretFile,
  ConfigSecretSource,
  ServarrApp,
} from '$lib/api';
import { emptyDraft, sameValue, withOp, withSecret, type ConfigDraft } from './changeSet';

/** The known apps, in the order the skill visits them (`SERVARR_APPS`). */
export const SERVARR_APPS: ServarrApp[] = ['sonarr', 'radarr', 'lidarr', 'prowlarr'];

/** Each app's name as the app spells it. */
export const APP_LABELS: Record<ServarrApp, string> = {
  sonarr: 'Sonarr',
  radarr: 'Radarr',
  lidarr: 'Lidarr',
  prowlarr: 'Prowlarr',
};

/** Each app's own default port, for the URL field's example. */
export const DEFAULT_PORTS: Record<ServarrApp, number> = {
  sonarr: 8989,
  radarr: 7878,
  lidarr: 8686,
  prowlarr: 9696,
};

export interface ServarrAppForm {
  /** Whether the app has a `[servarr.apps.<app>]` table. */
  enabled: boolean;
  url: string;
  /** A new key typed in this session; `""` keeps the stored one. */
  key: string;
  /** Clear the stored key, as Clear does on any other credential field. */
  clearKey: boolean;
  /** Whether a key is stored now; null for a table the loader dropped. */
  keySet: boolean | null;
  /** The stored table is missing its url or key, so neither is known. */
  incomplete: boolean;
  /** The key's secrets file, from the view's `secrets` entry; null when the
   *  view lists none. */
  keyFile: string | null;
  /** Where that file's value is held, for the status badge. */
  keySource: ConfigSecretSource | null;
  /** Why the key cannot be changed here (that entry's `read_only_reason`), or null. */
  keyReadOnly: string | null;
}

export type ServarrAppsForm = Record<ServarrApp, ServarrAppForm>;

const NO_KEY_FILE = 'This install has no secrets file for this key, so it is set on the host.';

/** The view's `secrets` entry for an app's key, matched by its config key. */
function keyEntry(secrets: ConfigSecretFile[], app: ServarrApp): ConfigSecretFile | undefined {
  return secrets.find((entry) => entry.config_key === `servarr.apps.${app}.api_key`);
}

export function servarrAppsForm(
  connection: AdminServarrConnection,
  secrets: ConfigSecretFile[],
): ServarrAppsForm {
  const apps = {} as ServarrAppsForm;
  for (const name of SERVARR_APPS) {
    const stored = connection.apps.find((entry) => entry.app === name);
    const file = keyEntry(secrets, name);
    apps[name] = {
      enabled: !!stored,
      url: stored?.url ?? '',
      key: '',
      clearKey: false,
      keySet: stored ? stored.api_key_set : false,
      incomplete: stored?.incomplete ?? false,
      keyFile: file?.name ?? null,
      keySource: file?.source ?? null,
      keyReadOnly: file ? file.read_only_reason : NO_KEY_FILE,
    };
  }
  return apps;
}

function normalHost(host: string): string {
  return host
    .toLowerCase()
    .replace(/^\[|\]$/g, '')
    .replace(/\.$/, '');
}

/**
 * Whether an IP literal is certainly not public. Only ranges that are never
 * routable are named, so a miss leaves the call to doctor and the skill
 * (`net_guard.ip_is_public`) rather than refusing a public address here.
 */
function privateAddress(host: string): boolean {
  const v4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    return (
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168)
    );
  }
  if (!host.includes(':')) return false;
  return host === '::1' || /^f[cd][0-9a-f]{0,2}:/.test(host) || /^fe[89ab][0-9a-f]?:/.test(host);
}

/** A name with no dot, or under a suffix only a local network answers for. */
function localName(host: string): boolean {
  if (host.includes(':') || /^[\d.]+$/.test(host)) return false;
  return !host.includes('.') || /\.(local|lan|home|internal|localdomain|home\.arpa)$/.test(host);
}

/**
 * The host as typed, the way the skill's `urlsplit(...).hostname` reads it:
 * no userinfo or port, brackets dropped, lowercased, no trailing dot. The
 * browser's own hostname is canonicalised (`[fd00:0::5]`, `10.1`, punycode)
 * and would never match the exact entry the skill compares against.
 */
function typedHost(url: string): string {
  const authority = url
    .trim()
    .replace(/^[a-z][a-z0-9+.-]*:\/\//i, '')
    .split(/[/?#]/, 1)[0];
  const hostPort = authority.slice(authority.lastIndexOf('@') + 1);
  const host = hostPort.startsWith('[')
    ? hostPort.slice(1, hostPort.indexOf(']'))
    : hostPort.replace(/:\d*$/, '');
  return host.toLowerCase().replace(/\.$/, '');
}

/**
 * The hosts `[private_hosts]` grants to `consumer` once the pending op lands,
 * or null while a whole-table `set_toml` is pending: that op holds TOML text,
 * which the server parses and the page cannot read.
 */
export function hostsGrantedTo(
  consumer: string,
  field: AdminConfigField | undefined,
  op: ConfigOp | undefined,
): string[] | null {
  if (op?.op === 'set_toml') return null;
  if (op?.op === 'unset') return [];
  const table = op?.op === 'set' ? op.value : field?.value;
  if (!table || typeof table !== 'object' || Array.isArray(table)) return [];
  const hosts: string[] = [];
  for (const [host, consumers] of Object.entries(table)) {
    if (Array.isArray(consumers) && consumers.includes(consumer)) hosts.push(host);
  }
  return hosts;
}

function grantAdvice(host: string): string {
  return `Add "${host}" = ["servarr"] to [private_hosts] under General`;
}

export interface PrivateHostNeed {
  host: string;
  /** True when the skill refuses the URL without it; false when it likely will. */
  required: boolean;
  message: string;
}

/**
 * The grant a URL needs in `[private_hosts]`, or null. Plain HTTP and a private
 * IP literal are refused without it, as the skill's guard 2 refuses them; a
 * local-looking name only probably resolves to a private address, so it is a
 * warning rather than a refusal. Null `privateHosts` means the grants cannot be
 * read (`hostsGrantedTo`), so nothing is said: the host may be in the pending
 * table, and `apply --check` and the skill still judge the URL.
 */
export function privateHostNeed(
  url: string,
  privateHosts: string[] | null,
): PrivateHostNeed | null {
  if (privateHosts === null) return null;
  let parsed: URL;
  try {
    parsed = new URL(url.trim());
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  const host = typedHost(url);
  if (!host) return null;
  if (new Set(privateHosts.map(normalHost)).has(host)) return null;
  if (parsed.protocol === 'http:') {
    return {
      host,
      required: true,
      message: `Plain HTTP goes only to a host granted to servarr. ${grantAdvice(host)}, or use https://.`,
    };
  }
  if (privateAddress(host)) {
    return {
      host,
      required: true,
      message: `${host} is a private address, which the skill reaches only with a grant. ${grantAdvice(host)}.`,
    };
  }
  if (localName(host)) {
    return {
      host,
      required: false,
      message: `${host} looks like a local name, and a private address needs a grant. ${grantAdvice(host)} if it resolves to one.`,
    };
  }
  return null;
}

/** What is wrong with the URL, mirroring the skill's checks before a request. */
function urlProblem(url: string, privateHosts: string[] | null): string | null {
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
  const need = privateHostNeed(text, privateHosts);
  return need?.required ? need.message : null;
}

/**
 * Problems keyed by `<app>.url` or `<app>.key` for the apps this change
 * touches; empty when it can be reviewed. An app left as it was is not
 * judged, so a stored problem doctor already reports does not block an
 * unrelated change.
 */
export function appProblems(
  form: ServarrAppsForm,
  initial: ServarrAppsForm,
  privateHosts: string[] | null,
): Record<string, string> {
  const problems: Record<string, string> = {};
  for (const name of SERVARR_APPS) {
    const app = form[name];
    if (!app.enabled || sameValue(app, initial[name])) continue;
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
export function servarrAppsDraft(form: ServarrAppsForm, initial: ServarrAppsForm): ConfigDraft {
  let draft = emptyDraft();
  const op = (next: ConfigOp) => {
    draft = withOp(draft, next);
  };
  for (const name of SERVARR_APPS) {
    const app = form[name];
    const before = initial[name];
    const path = ['servarr', 'apps', name];
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
    if (keyFile && app.clearKey) draft = withSecret(draft, keyFile, '');
    else if (keyFile && app.key !== '') draft = withSecret(draft, keyFile, app.key);
  }
  return draft;
}

function pick<T>(edited: T, before: T, after: T): T {
  return sameValue(edited, before) ? after : edited;
}

/**
 * `form` moved onto `after`, built from a config that changed under the page:
 * what the admin changed from `before` is kept, the rest is read from the new
 * values, so a review does not revert somebody else's change.
 */
export function rebaseApps(
  form: ServarrAppsForm,
  before: ServarrAppsForm,
  after: ServarrAppsForm,
): ServarrAppsForm {
  const apps = {} as ServarrAppsForm;
  for (const name of SERVARR_APPS) {
    const edited = form[name];
    const was = before[name];
    const now = after[name];
    apps[name] = {
      ...now,
      enabled: pick(edited.enabled, was.enabled, now.enabled),
      url: pick(edited.url, was.url, now.url),
      key: edited.key,
      clearKey: edited.clearKey,
    };
  }
  return apps;
}
