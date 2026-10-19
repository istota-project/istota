import { describe, expect, it } from 'vitest';
import type { AdminConfigView } from '$lib/api';
import {
  buildChangeSet,
  changeCount,
  displayValue,
  emptyDraft,
  opFor,
  retainExisting,
  withAdmins,
  withMove,
  withOp,
  withRaw,
  withSecret,
} from './changeSet';
import { configField, configSecret, configView } from '$lib/test/adminConfig';

describe('the draft', () => {
  it('addresses a field by its path, so a dotted segment is its own key', () => {
    let draft = emptyDraft();
    draft = withOp(draft, {
      op: 'set',
      path: ['brain', 'native', 'model_overrides', 'a.b'],
      value: 1,
    });
    draft = withOp(draft, {
      op: 'set',
      path: ['brain', 'native', 'model_overrides', 'a', 'b'],
      value: 2,
    });
    expect(changeCount(draft)).toBe(2);
  });

  it('replaces an op at the same path and drops it on null', () => {
    let draft = withOp(emptyDraft(), { op: 'set', path: ['email', 'imap_host'], value: 'a' });
    draft = withOp(draft, { op: 'unset', path: ['email', 'imap_host'] });
    expect(buildChangeSet(draft, 'd').ops).toEqual([{ op: 'unset', path: ['email', 'imap_host'] }]);
    draft = withOp(draft, null, ['email', 'imap_host']);
    expect(changeCount(draft)).toBe(0);
  });

  it('counts credentials and the admin list as changes', () => {
    let draft = withSecret(emptyDraft(), 'istota_email_imap_password', 'pw');
    draft = withSecret(draft, 'istota_developer_github_token', '');
    draft = withAdmins(draft, ['alice']);
    expect(changeCount(draft)).toBe(3);
    draft = withSecret(draft, 'istota_developer_github_token', null);
    draft = withAdmins(draft, null);
    expect(changeCount(draft)).toBe(1);
  });

  it('builds the wire change set, admins only when changed', () => {
    let draft = withOp(emptyDraft(), { op: 'set', path: ['email', 'imap_host'], value: 'h' });
    draft = withSecret(draft, 'istota_email_imap_password', '');
    expect(buildChangeSet(draft, 'abc')).toEqual({
      base_digest: 'abc',
      ops: [{ op: 'set', path: ['email', 'imap_host'], value: 'h' }],
      raw: null,
      secrets: { istota_email_imap_password: '' },
    });
    expect(buildChangeSet(withAdmins(draft, ['alice', 'bob']), 'abc').admins).toEqual([
      'alice',
      'bob',
    ]);
  });
});

describe('opFor', () => {
  const field = configField({ path: ['email', 'imap_host'], value: 'old', in_file: true });

  it('is a set for a new value', () => {
    expect(opFor(field, 'new')).toEqual({ op: 'set', path: ['email', 'imap_host'], value: 'new' });
  });

  it('is nothing when the value is what the field already holds', () => {
    expect(opFor(field, 'old')).toBeNull();
    expect(opFor(configField({ value: null }), '')).toBeNull();
  });
});

describe('displayValue', () => {
  const field = configField({ value: 5, default: 3, in_file: true });

  it('shows the pending value, the default after a reset, or the loaded value', () => {
    expect(displayValue(field, { op: 'set', path: field.path, value: 9 })).toBe(9);
    expect(displayValue(field, { op: 'unset', path: field.path })).toBe(3);
    expect(displayValue(field, undefined)).toBe(5);
  });
});

describe('retainExisting', () => {
  it('keeps the changes whose field is still editable and drops the rest', () => {
    const view: AdminConfigView = configView([
      configField({ path: ['email', 'imap_host'] }),
      configField({ path: ['web', 'port'], editor: 'readonly', read_only_reason: 'host' }),
      configField({
        path: ['email', 'imap_password'],
        editor: 'secret',
        credential: { kind: 'file', name: 'istota_email_imap_password' },
      }),
    ]);
    let draft = withOp(emptyDraft(), { op: 'set', path: ['email', 'imap_host'], value: 'h' });
    draft = withOp(draft, { op: 'set', path: ['web', 'port'], value: 1 });
    draft = withOp(draft, { op: 'set', path: ['gone', 'key'], value: 1 });
    draft = withSecret(draft, 'istota_email_imap_password', 'pw');
    draft = withSecret(draft, 'istota_unknown', 'pw');
    draft = withAdmins(draft, ['alice']);

    const { draft: kept, dropped } = retainExisting(draft, view);
    expect(buildChangeSet(kept, 'd')).toEqual({
      base_digest: 'd',
      ops: [{ op: 'set', path: ['email', 'imap_host'], value: 'h' }],
      raw: null,
      secrets: { istota_email_imap_password: 'pw' },
      admins: ['alice'],
    });
    expect(dropped).toBe(3);
  });

  it('drops the admin list when it became read-only', () => {
    const view = configView([]);
    view.admins.read_only_reason = 'Managed by ansible';
    const { draft, dropped } = retainExisting(withAdmins(emptyDraft(), ['alice']), view);
    expect(draft.admins).toBeNull();
    expect(dropped).toBe(1);
  });
});

describe('raw text, tables and removals', () => {
  it('sends raw text as the whole file and counts it as one change', () => {
    const draft = withRaw(emptyDraft(), 'bot_name = "x"\n');
    expect(changeCount(draft)).toBe(1);
    expect(buildChangeSet(draft, 'd')).toEqual({
      base_digest: 'd',
      ops: [],
      raw: 'bot_name = "x"\n',
      secrets: {},
    });
    expect(changeCount(withRaw(draft, null))).toBe(0);
  });

  it('keeps a table edit and a removal the new file still needs', () => {
    const view = configView(
      [configField({ path: ['models', 'aliases'], editor: 'table', toml: '' })],
      {
        problems: [
          {
            kind: 'unknown',
            path: ['web', 'typo_key'],
            message: 'not a recognised key',
            blocking: false,
            action: 'unset',
          },
          { kind: 'unknown', path: ['web', 'other'], message: 'm', blocking: false },
        ],
      },
    );
    let draft = withOp(emptyDraft(), {
      op: 'set_toml',
      path: ['models', 'aliases'],
      toml: '[aliases]\nfast = "haiku"\n',
    });
    draft = withOp(draft, { op: 'unset', path: ['web', 'typo_key'] });
    draft = withOp(draft, { op: 'unset', path: ['web', 'other'] });
    const { draft: kept, dropped } = retainExisting(draft, view);
    expect(buildChangeSet(kept, 'd').ops).toEqual([
      { op: 'set_toml', path: ['models', 'aliases'], toml: '[aliases]\nfast = "haiku"\n' },
      { op: 'unset', path: ['web', 'typo_key'] },
    ]);
    expect(dropped).toBe(1);
  });

  it('drops raw text written against a file that changed', () => {
    const { draft, dropped } = retainExisting(withRaw(emptyDraft(), 'a = 1\n'), configView([]));
    expect(draft.raw).toBeNull();
    expect(dropped).toBe(1);
  });

  it('keeps a move while the credential is still inline and editable', () => {
    const view = configView([], {
      secrets: [
        configSecret({
          name: 'istota_sms_twilio_auth_token',
          config_key: 'sms.twilio.auth_token',
          source: 'inline',
        }),
        configSecret({
          name: 'istota_whatsapp_access_token',
          config_key: 'whatsapp.cloud.access_token',
          source: 'file',
        }),
        configSecret({
          name: 'istota_whatsapp_app_secret',
          config_key: 'whatsapp.cloud.app_secret',
          source: 'inline',
          read_only_reason: 'managed by ansible',
        }),
      ],
    });
    let draft = withMove(emptyDraft(), 'sms.twilio.auth_token', true);
    draft = withMove(draft, 'whatsapp.cloud.access_token', true);
    draft = withMove(draft, 'whatsapp.cloud.app_secret', true);
    const { draft: kept, dropped } = retainExisting(draft, view);
    expect(kept.move_to_file).toEqual(['sms.twilio.auth_token']);
    expect(dropped).toBe(2);
  });

  it('keeps a move the new file still offers as a problem action', () => {
    const view = configView([], {
      problems: [
        {
          kind: 'warning',
          path: ['sms', 'twilio', 'auth_token'],
          message: 'stored in config.toml',
          blocking: false,
          action: 'move',
        },
      ],
    });
    const { draft, dropped } = retainExisting(
      withMove(emptyDraft(), 'sms.twilio.auth_token', true),
      view,
    );
    expect(draft.move_to_file).toEqual(['sms.twilio.auth_token']);
    expect(dropped).toBe(0);
  });
});

describe('moving a credential into its secret file', () => {
  it('names the label once, counts it, and takes it back', () => {
    let draft = withMove(emptyDraft(), 'sms.twilio.auth_token', true);
    draft = withMove(draft, 'sms.twilio.auth_token', true);
    expect(draft.move_to_file).toEqual(['sms.twilio.auth_token']);
    expect(changeCount(draft)).toBe(1);
    draft = withMove(draft, 'sms.twilio.auth_token', false);
    expect(draft.move_to_file).toEqual([]);
    expect(changeCount(draft)).toBe(0);
  });

  it('sends move_to_file with no value and nothing in secrets or ops', () => {
    const changeSet = buildChangeSet(withMove(emptyDraft(), 'sms.twilio.auth_token', true), 'abc');
    expect(changeSet).toEqual({
      base_digest: 'abc',
      ops: [],
      raw: null,
      secrets: {},
      move_to_file: ['sms.twilio.auth_token'],
    });
  });

  it('leaves move_to_file off a change set with no moves', () => {
    expect(buildChangeSet(emptyDraft(), 'abc')).not.toHaveProperty('move_to_file');
  });
});
