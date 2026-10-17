import { describe, expect, it } from 'vitest';

import type {
  AdminConfigView,
  AdminServarrApp,
  AdminServarrConnection,
  ConfigManagedEntry,
  ConfigSecretFile,
} from '$lib/api';
import { buildChangeSet } from '$lib/admin/config/changeSet';
import { configSecret } from '$lib/test/adminConfig';
import {
  formProblems,
  privateHostNeed,
  rebaseForm,
  servarrDraft,
  servarrForm,
  servarrReadOnlyReason,
  viewPrivateHosts,
  withPrivateHost,
  type ServarrForm,
} from './servarrForm';

/**
 * The servarr card's form as plain data: built from the connection payload,
 * checked before review, and turned into a config-editor change set.
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
    requesters: ['bob'],
    defaults: { radarr: { root_folder: '/films' } },
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
): ServarrForm {
  return servarrForm(connection(over), HOSTS, secrets);
}

function draftOf(edited: ServarrForm, initial: ServarrForm = form()) {
  return buildChangeSet(servarrDraft(edited, initial), 'digest');
}

describe('servarrForm', () => {
  it('starts every key empty, whatever is stored', () => {
    const initial = form();
    expect(initial.apps.radarr).toMatchObject({
      enabled: true,
      url: 'http://192.0.2.10:7878',
      key: '',
      keySet: true,
    });
    expect(initial.apps.sonarr).toMatchObject({ enabled: false, url: '', key: '', keySet: false });
    expect(initial.requesters).toEqual(['bob']);
    expect(initial.privateHosts).toBe('192.0.2.10');
    expect(initial.defaults.radarr.root_folder).toBe('/films');
    expect(initial.defaults.radarr.quality_profile).toBe('');
  });
});

describe('servarrDraft', () => {
  it('is empty for an untouched form', () => {
    const changeSet = draftOf(form());
    expect(changeSet.ops).toEqual([]);
    expect(changeSet.secrets).toEqual({});
  });

  it('sends no credential for an untouched key', () => {
    const edited = form();
    edited.apps.radarr.url = 'http://192.0.2.10:7879';
    const changeSet = draftOf(edited);
    expect(changeSet.secrets).toEqual({});
    expect(changeSet.ops).toEqual([
      { op: 'set', path: ['servarr', 'apps', 'radarr', 'url'], value: 'http://192.0.2.10:7879' },
    ]);
  });

  it('sends a replaced key as one credential and in no op', () => {
    const edited = form();
    edited.apps.radarr.key = 'fixture-arr-key-1';
    const changeSet = draftOf(edited);
    expect(changeSet.secrets).toEqual({ [RADARR_FILE]: 'fixture-arr-key-1' });
    expect(Object.keys(changeSet.secrets).some((name) => name.includes('.'))).toBe(false);
    expect(JSON.stringify(changeSet.ops)).not.toContain('fixture-arr-key-1');
  });

  it('adds a new app as its URL and a credential', () => {
    const edited = form();
    edited.apps.sonarr = { ...edited.apps.sonarr, enabled: true, url: 'http://192.0.2.10:8989' };
    edited.apps.sonarr.key = 'fixture-arr-key-2';
    const changeSet = draftOf(edited);
    expect(changeSet.ops).toEqual([
      { op: 'set', path: ['servarr', 'apps', 'sonarr', 'url'], value: 'http://192.0.2.10:8989' },
    ]);
    expect(changeSet.secrets).toEqual({ istota_servarr_sonarr_api_key: 'fixture-arr-key-2' });
  });

  it('removes the table of an app turned off and clears its key file', () => {
    const edited = form();
    edited.apps.radarr.enabled = false;
    edited.apps.radarr.key = 'typed-then-turned-off';
    const changeSet = draftOf(edited);
    expect(changeSet.ops).toEqual([{ op: 'unset', path: ['servarr', 'apps', 'radarr'] }]);
    expect(changeSet.secrets).toEqual({ [RADARR_FILE]: '' });
  });

  it('leaves a read-only key file alone, whatever the form holds', () => {
    const reason = 'Set by your deployment.';
    const initial = form({}, secretFiles({ read_only_reason: reason }));
    expect(initial.apps.radarr.keyReadOnly).toBe(reason);
    const edited = structuredClone(initial);
    edited.apps.radarr.key = 'fixture-arr-key-6';
    expect(draftOf(edited, initial).secrets).toEqual({});
    edited.apps.radarr.enabled = false;
    expect(draftOf(edited, initial).secrets).toEqual({});
  });

  it('treats a key with no secrets file in the view as read-only', () => {
    const initial = form({}, []);
    expect(initial.apps.radarr.keyFile).toBeNull();
    expect(initial.apps.radarr.keyReadOnly).toBeTruthy();
    const edited = structuredClone(initial);
    edited.apps.radarr.key = 'fixture-arr-key-7';
    expect(draftOf(edited, initial).secrets).toEqual({});
  });

  it('sets requesters, private hosts and defaults, and unsets what is emptied', () => {
    const edited = form();
    edited.requesters = [];
    edited.privateHosts = '192.0.2.10\narr.example.com\n';
    edited.defaults.radarr.root_folder = '';
    edited.defaults.radarr.quality_profile = ' HD ';
    const changeSet = draftOf(edited);
    expect(changeSet.ops).toEqual([
      { op: 'unset', path: ['servarr', 'requesters'] },
      { op: 'set', path: ['servarr', 'private_hosts'], value: ['192.0.2.10', 'arr.example.com'] },
      { op: 'set', path: ['servarr', 'defaults', 'radarr', 'quality_profile'], value: 'HD' },
      { op: 'unset', path: ['servarr', 'defaults', 'radarr', 'root_folder'] },
    ]);
  });

  it('rewrites the URL of an incomplete table, whose stored URL is unknown', () => {
    const initial = form({
      apps: [app({ url: '', api_key_set: null, incomplete: true, problem: 'dropped' })],
    });
    const edited = structuredClone(initial);
    edited.apps.radarr.url = 'http://192.0.2.10:7878';
    edited.apps.radarr.key = 'fixture-arr-key-3';
    const changeSet = draftOf(edited, initial);
    expect(changeSet.ops).toEqual([
      { op: 'set', path: ['servarr', 'apps', 'radarr', 'url'], value: 'http://192.0.2.10:7878' },
    ]);
    expect(changeSet.secrets).toEqual({ [RADARR_FILE]: 'fixture-arr-key-3' });
  });
});

describe('formProblems', () => {
  it('passes a stored app as it is', () => {
    expect(formProblems(form())).toEqual({});
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
      edited.apps.radarr.url = url;
      expect(formProblems(edited)['radarr.url'], url).toBeTruthy();
    }
  });

  it('refuses plain HTTP to a host not in the private hosts, as the skill does', () => {
    const edited = form();
    edited.apps.radarr.url = 'http://arr.example.com:7878';
    expect(formProblems(edited)['radarr.url']).toMatch(/private hosts/);
    edited.privateHosts = '192.0.2.10\nARR.example.com.';
    expect(formProblems(edited)).toEqual({});
    edited.apps.radarr.url = 'https://arr.example.org';
    expect(formProblems(edited)).toEqual({});
  });

  it('refuses HTTPS to a private IP literal not in the private hosts, as guard 2 does', () => {
    const edited = form();
    edited.apps.radarr.url = 'https://10.1.2.3:7878';
    expect(formProblems(edited)['radarr.url']).toMatch(/private address/);
    edited.privateHosts = '10.1.2.3';
    expect(formProblems(edited)).toEqual({});
  });

  it('only warns about a local-looking name, which may resolve publicly', () => {
    const edited = form();
    edited.apps.radarr.url = 'https://nas.local:7878';
    expect(formProblems(edited)).toEqual({});
  });

  it('asks for a key for an app with none stored', () => {
    const edited = form();
    edited.apps.sonarr = { ...edited.apps.sonarr, enabled: true, url: 'http://192.0.2.10:8989' };
    expect(formProblems(edited)['sonarr.key']).toBeTruthy();
    edited.apps.sonarr.key = 'fixture-arr-key-4';
    expect(formProblems(edited)).toEqual({});
  });

  it('says why a missing key cannot be entered here when its file is read-only', () => {
    const reason = 'Set by your deployment.';
    const edited = form({}, secretFiles({ read_only_reason: reason }));
    edited.apps.sonarr = { ...edited.apps.sonarr, enabled: true, url: 'http://192.0.2.10:8989' };
    expect(formProblems(edited)['sonarr.key']).toContain(reason);
  });

  it('ignores an app turned off', () => {
    const edited = form();
    edited.apps.radarr = { ...edited.apps.radarr, enabled: false, url: 'nonsense' };
    expect(formProblems(edited)).toEqual({});
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
      expect(privateHostNeed(url, ''), url).toMatchObject({ host, required: true });
    }
  });

  it('warns, without requiring, for a name only a local network answers for', () => {
    for (const url of ['https://sonarr:8989', 'https://nas.lan', 'https://box.home.arpa']) {
      expect(privateHostNeed(url, ''), url).toMatchObject({ required: false });
    }
  });

  it('names the host as typed, which is what the skill compares', () => {
    expect(privateHostNeed('http://[fd00:0:0:0:0:0:0:5]:7878', '')?.host).toBe(
      'fd00:0:0:0:0:0:0:5',
    );
    expect(privateHostNeed('http://user@NAS.lan.:8989/', '')?.host).toBe('nas.lan');
    expect(privateHostNeed('http://[fd00:0:0:0:0:0:0:5]:7878', 'fd00::5')).not.toBeNull();
  });

  it('needs nothing for a public HTTPS host, a listed host or an unparseable URL', () => {
    expect(privateHostNeed('https://arr.example.com', '')).toBeNull();
    expect(privateHostNeed('https://172.32.0.1', '')).toBeNull();
    expect(privateHostNeed('http://Arr.Example.com:7878', 'arr.example.com.')).toBeNull();
    expect(privateHostNeed('not a url', '')).toBeNull();
  });
});

describe('withPrivateHost', () => {
  it('appends a host once', () => {
    const edited = form();
    expect(withPrivateHost(edited, '10.0.0.9')).toBe('192.0.2.10\n10.0.0.9');
    edited.privateHosts = '192.0.2.10\n10.0.0.9';
    expect(withPrivateHost(edited, '10.0.0.9')).toBe('192.0.2.10\n10.0.0.9');
  });
});

describe('rebaseForm', () => {
  it('keeps what the admin changed and takes the new file for the rest', () => {
    const before = form();
    const edited = structuredClone(before);
    edited.apps.radarr.key = 'fixture-arr-key-5';
    edited.defaults.radarr.quality_profile = 'HD';
    const after = servarrForm(
      connection({ requesters: ['bob', 'carol'], defaults: { radarr: { root_folder: '/new' } } }),
      HOSTS,
      secretFiles(),
    );
    const rebased = rebaseForm(edited, before, after);
    expect(rebased.apps.radarr.key).toBe('fixture-arr-key-5');
    expect(rebased.defaults.radarr.quality_profile).toBe('HD');
    expect(rebased.defaults.radarr.root_folder).toBe('/new');
    expect(rebased.requesters).toEqual(['bob', 'carol']);
  });
});

function view(over: Partial<AdminConfigView> = {}): AdminConfigView {
  const unmanaged: ConfigManagedEntry = { managed: false, source: null, at: null };
  return {
    config_path: '/data/config/config.toml',
    editable: true,
    sections: [
      {
        key: 'servarr',
        label: '[servarr]',
        fields: [
          {
            key: 'servarr.private_hosts',
            name: 'private_hosts',
            value: ['192.0.2.10'],
            type: 'list[str]',
            secret: false,
            set: true,
            path: ['servarr', 'private_hosts'],
            in_file: true,
            choices: null,
            multi_choices: null,
            doc: null,
            credential: null,
            env_override: null,
            editor: 'list',
            read_only_reason: null,
            risky: false,
          },
        ],
      },
    ],
    applier: { kind: 'host', available: true, reason: '' },
    managed: { config: unmanaged, secrets: unmanaged, admins: unmanaged },
    document_digest: 'digest',
    started_at: null,
    problems: [],
    has_comments: false,
    raw_toml: '',
    secrets: [],
    admins: { users: ['alice'], read_only_reason: null },
    ...over,
  };
}

describe('servarrReadOnlyReason', () => {
  it('is null for an editable, unmanaged config', () => {
    expect(servarrReadOnlyReason(view())).toBeNull();
    expect(viewPrivateHosts(view())).toEqual(['192.0.2.10']);
  });

  it('gives the applier reason when there is no applier', () => {
    const reason = 'This install has no config applier.';
    expect(
      servarrReadOnlyReason(
        view({ editable: false, applier: { kind: 'none', available: false, reason } }),
      ),
    ).toBe(reason);
  });

  it('names the plan that manages config.toml', () => {
    const unmanaged: ConfigManagedEntry = { managed: false, source: null, at: null };
    const reason = servarrReadOnlyReason(
      view({
        managed: {
          config: { managed: true, source: 'ansible', at: null },
          secrets: unmanaged,
          admins: unmanaged,
        },
      }),
    );
    expect(reason).toMatch(/ansible/);
    expect(reason).toMatch(/settings plan/);
  });
});
