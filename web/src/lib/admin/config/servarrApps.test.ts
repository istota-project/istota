import { describe, expect, it } from 'vitest';

import type { AdminServarrApp, AdminServarrConnection, ConfigSecretFile } from '$lib/api';
import { buildChangeSet } from '$lib/admin/config/changeSet';
import { configField, configSecret } from '$lib/test/adminConfig';
import {
  appProblems,
  hostsGrantedTo,
  privateHostNeed,
  rebaseApps,
  servarrAppsDraft,
  servarrAppsForm,
  type ServarrAppsForm,
} from './servarrApps';

/**
 * The Configuration page's `[servarr.apps]` editor as plain data: built from
 * the connection payload, checked before review, and turned into a
 * config-editor change set.
 */

function app(over: Partial<AdminServarrApp> = {}): AdminServarrApp {
  return {
    app: 'radarr',
    url: 'http://192.0.2.10:7878',
    api_key_set: true,
    incomplete: false,
    problem: null,
    check: null,
    ...over,
  };
}

function connection(over: Partial<AdminServarrConnection> = {}): AdminServarrConnection {
  return {
    id: 'servarr',
    label: 'Servarr',
    status: 'configured',
    apps: [app()],
    missing: ['sonarr', 'lidarr', 'prowlarr'],
    requesters: [],
    defaults: {},
    ...over,
  };
}

const HOSTS = ['192.0.2.10'];

/** The view's secrets entries for the four keys, as the Configuration page has them. */
function secretFiles(over: Partial<ConfigSecretFile> = {}): ConfigSecretFile[] {
  return ['sonarr', 'radarr', 'lidarr', 'prowlarr'].map((name) =>
    configSecret({
      name: `istota_servarr_${name}_api_key`,
      config_key: `servarr.apps.${name}.api_key`,
      set: name === 'radarr',
      source: name === 'radarr' ? 'file' : 'none',
      ...over,
    }),
  );
}

const RADARR_FILE = 'istota_servarr_radarr_api_key';

function form(
  over: Partial<AdminServarrConnection> = {},
  secrets: ConfigSecretFile[] = secretFiles(),
): ServarrAppsForm {
  return servarrAppsForm(connection(over), secrets);
}

function changeOf(edited: ServarrAppsForm, initial: ServarrAppsForm = form()) {
  return buildChangeSet(servarrAppsDraft(edited, initial), 'digest');
}

describe('servarrAppsForm', () => {
  it('starts every key empty, whatever is stored, and carries where it is held', () => {
    const initial = form();
    expect(initial.radarr).toMatchObject({
      enabled: true,
      url: 'http://192.0.2.10:7878',
      key: '',
      keySet: true,
      keyFile: RADARR_FILE,
      keySource: 'file',
    });
    expect(initial.sonarr).toMatchObject({ enabled: false, url: '', key: '', keySet: false });
  });

  it('treats a key with no secrets file in the view as read-only', () => {
    const initial = form({}, []);
    expect(initial.radarr.keyFile).toBeNull();
    expect(initial.radarr.keyReadOnly).toBeTruthy();
  });
});

describe('servarrAppsDraft', () => {
  it('is empty for an untouched form', () => {
    const changeSet = changeOf(form());
    expect(changeSet.ops).toEqual([]);
    expect(changeSet.secrets).toEqual({});
  });

  it('sends a changed URL as an op and no credential for an untouched key', () => {
    const edited = form();
    edited.radarr.url = 'http://192.0.2.10:7879';
    const changeSet = changeOf(edited);
    expect(changeSet.secrets).toEqual({});
    expect(changeSet.ops).toEqual([
      { op: 'set', path: ['servarr', 'apps', 'radarr', 'url'], value: 'http://192.0.2.10:7879' },
    ]);
  });

  it('sends a replaced key as one credential and in no op', () => {
    const edited = form();
    edited.radarr.key = 'fixture-arr-key-1';
    const changeSet = changeOf(edited);
    expect(changeSet.secrets).toEqual({ [RADARR_FILE]: 'fixture-arr-key-1' });
    expect(JSON.stringify(changeSet.ops)).not.toContain('fixture-arr-key-1');
  });

  it('clears a stored key with an empty credential and leaves the table', () => {
    const edited = form();
    edited.radarr.clearKey = true;
    const changeSet = changeOf(edited);
    expect(changeSet.secrets).toEqual({ [RADARR_FILE]: '' });
    expect(changeSet.ops).toEqual([]);
  });

  it('adds a new app as its URL and a credential', () => {
    const edited = form();
    edited.sonarr = { ...edited.sonarr, enabled: true, url: 'http://192.0.2.10:8989' };
    edited.sonarr.key = 'fixture-arr-key-2';
    const changeSet = changeOf(edited);
    expect(changeSet.ops).toEqual([
      { op: 'set', path: ['servarr', 'apps', 'sonarr', 'url'], value: 'http://192.0.2.10:8989' },
    ]);
    expect(changeSet.secrets).toEqual({ istota_servarr_sonarr_api_key: 'fixture-arr-key-2' });
  });

  it('removes the table of an app turned off and clears its key file', () => {
    const edited = form();
    edited.radarr.enabled = false;
    edited.radarr.key = 'typed-then-turned-off';
    const changeSet = changeOf(edited);
    expect(changeSet.ops).toEqual([{ op: 'unset', path: ['servarr', 'apps', 'radarr'] }]);
    expect(changeSet.secrets).toEqual({ [RADARR_FILE]: '' });
  });

  it('leaves a read-only key file alone, whatever the form holds', () => {
    const reason = 'Set by your deployment.';
    const initial = form({}, secretFiles({ read_only_reason: reason }));
    const edited = structuredClone(initial);
    edited.radarr.key = 'fixture-arr-key-6';
    expect(changeOf(edited, initial).secrets).toEqual({});
    edited.radarr.enabled = false;
    expect(changeOf(edited, initial).secrets).toEqual({});
  });

  it('rewrites the URL of an incomplete table, whose stored URL is unknown', () => {
    const initial = form({
      apps: [app({ url: '', api_key_set: null, incomplete: true, problem: 'dropped' })],
    });
    const edited = structuredClone(initial);
    edited.radarr.url = 'http://192.0.2.10:7878';
    edited.radarr.key = 'fixture-arr-key-3';
    const changeSet = changeOf(edited, initial);
    expect(changeSet.ops).toEqual([
      { op: 'set', path: ['servarr', 'apps', 'radarr', 'url'], value: 'http://192.0.2.10:7878' },
    ]);
    expect(changeSet.secrets).toEqual({ [RADARR_FILE]: 'fixture-arr-key-3' });
  });
});

describe('appProblems', () => {
  it('passes a stored app as it is, and does not judge an app left alone', () => {
    expect(appProblems(form(), form(), HOSTS)).toEqual({});
    const stale = form({ apps: [app({ url: 'http://arr.example.com:7878' })] });
    expect(appProblems(stale, stale, [])).toEqual({});
  });

  it('refuses a URL that is not http(s) with a host, or that carries userinfo', () => {
    for (const url of [
      'ftp://192.0.2.10',
      'radarr',
      'http://admin:pw@192.0.2.10:7878',
      '',
      'http:192.0.2.10:7878',
      'https://arr.example.com/?a=1',
      'https://arr.example.com/#top',
    ]) {
      const edited = form();
      edited.radarr.url = url;
      expect(appProblems(edited, form(), HOSTS)['radarr.url'], url).toBeTruthy();
    }
  });

  it('refuses plain HTTP to a host with no grant, saying what to write', () => {
    const edited = form();
    edited.radarr.url = 'http://arr.example.com:7878';
    expect(appProblems(edited, form(), HOSTS)['radarr.url']).toContain(
      'Add "arr.example.com" = ["servarr"] to [private_hosts] under General',
    );
    expect(appProblems(edited, form(), ['192.0.2.10', 'ARR.example.com.'])).toEqual({});
    edited.radarr.url = 'https://arr.example.org';
    expect(appProblems(edited, form(), HOSTS)).toEqual({});
  });

  it('refuses HTTPS to a private IP literal with no grant, as guard 2 does', () => {
    const edited = form();
    edited.radarr.url = 'https://10.1.2.3:7878';
    const problem = appProblems(edited, form(), HOSTS)['radarr.url'];
    expect(problem).toMatch(/private address/);
    expect(problem).toContain('Add "10.1.2.3" = ["servarr"] to [private_hosts] under General');
    expect(appProblems(edited, form(), ['10.1.2.3'])).toEqual({});
  });

  it('only warns about a local-looking name, which may resolve publicly', () => {
    const edited = form();
    edited.radarr.url = 'https://nas.local:7878';
    expect(appProblems(edited, form(), HOSTS)).toEqual({});
  });

  it('asks for a key for an app with none stored', () => {
    const edited = form();
    edited.sonarr = { ...edited.sonarr, enabled: true, url: 'http://192.0.2.10:8989' };
    expect(appProblems(edited, form(), HOSTS)['sonarr.key']).toBeTruthy();
    edited.sonarr.key = 'fixture-arr-key-4';
    expect(appProblems(edited, form(), HOSTS)).toEqual({});
  });

  it('says why a missing key cannot be entered here when its file is read-only', () => {
    const reason = 'Set by your deployment.';
    const initial = form({}, secretFiles({ read_only_reason: reason }));
    const edited = structuredClone(initial);
    edited.sonarr = { ...edited.sonarr, enabled: true, url: 'http://192.0.2.10:8989' };
    expect(appProblems(edited, initial, HOSTS)['sonarr.key']).toContain(reason);
  });

  it('ignores an app turned off', () => {
    const edited = form();
    edited.radarr = { ...edited.radarr, enabled: false, url: 'nonsense' };
    expect(appProblems(edited, form(), HOSTS)).toEqual({});
  });

  it('skips only the private-host refusal when the grants cannot be read', () => {
    const edited = form();
    edited.radarr.url = 'http://arr.example.com:7878';
    expect(appProblems(edited, form(), null)).toEqual({});
    edited.radarr.url = 'ftp://192.0.2.10';
    expect(appProblems(edited, form(), null)['radarr.url']).toMatch(/http/);
    edited.radarr.url = 'http://192.0.2.10:7878';
    edited.sonarr = { ...edited.sonarr, enabled: true, url: 'http://192.0.2.10:8989' };
    expect(appProblems(edited, form(), null)['sonarr.key']).toBeTruthy();
  });
});

describe('privateHostNeed', () => {
  it('requires the host for plain HTTP and a private IP literal', () => {
    for (const [url, host] of [
      ['http://arr.example.com:7878', 'arr.example.com'],
      ['https://192.168.1.10:8989', '192.168.1.10'],
      ['https://172.20.0.4', '172.20.0.4'],
      ['https://127.0.0.1:9696', '127.0.0.1'],
      ['https://[fd00::5]:7878', 'fd00::5'],
    ]) {
      expect(privateHostNeed(url, []), url).toMatchObject({ host, required: true });
    }
  });

  it('warns, without requiring, for a name only a local network answers for', () => {
    for (const url of ['https://sonarr:8989', 'https://nas.lan', 'https://box.home.arpa']) {
      expect(privateHostNeed(url, []), url).toMatchObject({ required: false });
    }
  });

  it('names the host as typed, which is what the skill compares', () => {
    expect(privateHostNeed('http://[fd00:0:0:0:0:0:0:5]:7878', [])?.host).toBe(
      'fd00:0:0:0:0:0:0:5',
    );
    expect(privateHostNeed('http://user@NAS.lan.:8989/', [])?.host).toBe('nas.lan');
    expect(privateHostNeed('http://[fd00:0:0:0:0:0:0:5]:7878', ['fd00::5'])).not.toBeNull();
  });

  it('needs nothing for a public HTTPS host, a listed host or an unparseable URL', () => {
    expect(privateHostNeed('https://arr.example.com', [])).toBeNull();
    expect(privateHostNeed('https://172.32.0.1', [])).toBeNull();
    expect(privateHostNeed('http://Arr.Example.com:7878', ['arr.example.com.'])).toBeNull();
    expect(privateHostNeed('not a url', [])).toBeNull();
  });

  it('says nothing while the grants cannot be read', () => {
    expect(privateHostNeed('http://arr.example.com:7878', null)).toBeNull();
    expect(privateHostNeed('https://nas.lan', null)).toBeNull();
  });
});

describe('hostsGrantedTo', () => {
  const table = configField({
    path: ['private_hosts'],
    type: 'dict',
    editor: 'table',
    value: {
      '10.0.0.5': ['servarr'],
      'wiki.example.internal': ['browser'],
      'rss.example.internal': ['browser', 'servarr'],
      'none.example.internal': [],
    },
  });

  it('reads the hosts naming the consumer, and no other', () => {
    expect(hostsGrantedTo('servarr', table, undefined)).toEqual([
      '10.0.0.5',
      'rss.example.internal',
    ]);
    expect(hostsGrantedTo('feeds', table, undefined)).toEqual([]);
  });

  it('is null while a whole-table edit is pending, whatever the table says now', () => {
    const op = { op: 'set_toml' as const, path: table.path, toml: '[private_hosts]\n' };
    expect(hostsGrantedTo('servarr', table, op)).toBeNull();
  });

  it('follows a reset and a set value over what the field holds', () => {
    expect(hostsGrantedTo('servarr', table, { op: 'unset', path: table.path })).toEqual([]);
    const value = { '10.0.0.9': ['servarr'] };
    expect(hostsGrantedTo('servarr', table, { op: 'set', path: table.path, value })).toEqual([
      '10.0.0.9',
    ]);
  });

  it('grants nothing for a missing field or a value of another shape', () => {
    expect(hostsGrantedTo('servarr', undefined, undefined)).toEqual([]);
    for (const value of [null, ['10.0.0.5'], { '10.0.0.5': 'servarr' }, 'servarr']) {
      const field = configField({ path: ['private_hosts'], value });
      expect(hostsGrantedTo('servarr', field, undefined)).toEqual([]);
    }
  });
});

describe('rebaseApps', () => {
  it('keeps what the admin changed and takes the new values for the rest', () => {
    const before = form();
    const edited = structuredClone(before);
    edited.radarr.key = 'fixture-arr-key-5';
    edited.sonarr = { ...edited.sonarr, enabled: true, url: 'https://sonarr.example.com' };
    const after = servarrAppsForm(
      connection({ apps: [app({ url: 'http://192.0.2.10:7880' })] }),
      secretFiles(),
    );
    const rebased = rebaseApps(edited, before, after);
    expect(rebased.radarr.key).toBe('fixture-arr-key-5');
    expect(rebased.radarr.url).toBe('http://192.0.2.10:7880');
    expect(rebased.sonarr).toMatchObject({ enabled: true, url: 'https://sonarr.example.com' });
  });
});
