import { describe, expect, it } from 'vitest';
import type { AdminConfigView } from '$lib/api';
import {
  buildChangeSet,
  changeCount,
  displayValue,
  emptyDraft,
  opFor,
  parseList,
  parseNumber,
  retainExisting,
  withAdmins,
  withOp,
  withSecret,
} from './changeSet';
import { configField, configView } from '$lib/test/adminConfig';

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

describe('parsing', () => {
  it('reads a number by kind and refuses what is not one', () => {
    expect(parseNumber('42', 'int')).toBe(42);
    expect(parseNumber('4.5', 'int')).toBeNull();
    expect(parseNumber('4.5', 'float')).toBe(4.5);
    expect(parseNumber('', 'float')).toBeNull();
    expect(parseNumber('abc', 'int')).toBeNull();
  });

  it('reads a list one item per line, numbers when the list held numbers', () => {
    expect(parseList('a\n\n b \n', false)).toEqual(['a', 'b']);
    expect(parseList('1\n2', true)).toEqual([1, 2]);
    expect(parseList('1\nx', true)).toBeNull();
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
