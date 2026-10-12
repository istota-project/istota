/**
 * Test builders for the config editor's view payload: one field with every
 * key the server sends, and a view around a list of them. Overrides are
 * partial so a test states only what it is about.
 */
import type { AdminConfigField, AdminConfigView, ConfigPreview, ConfigRequest } from '$lib/api';

export function configField(overrides: Partial<AdminConfigField> = {}): AdminConfigField {
  const path = overrides.path ?? ['email', 'imap_host'];
  const name = path[path.length - 1];
  return {
    key: path.join('.'),
    name,
    value: '',
    type: 'str',
    secret: false,
    set: false,
    path,
    default: '',
    in_file: false,
    choices: null,
    multi_choices: null,
    doc: null,
    credential: null,
    env_override: null,
    editor: 'str',
    read_only_reason: null,
    risky: false,
    ...overrides,
  };
}

export function configView(
  fields: AdminConfigField[],
  overrides: Partial<AdminConfigView> = {},
): AdminConfigView {
  const bySection = new Map<string, AdminConfigField[]>();
  for (const field of fields) {
    const section = field.path.length > 1 ? field.path.slice(0, -1).join('.') : 'general';
    bySection.set(section, [...(bySection.get(section) ?? []), field]);
  }
  return {
    config_path: '/data/config/config.toml',
    editable: true,
    sections: [...bySection].map(([key, sectionFields]) => ({
      key,
      label: key === 'general' ? 'General' : `[${key}]`,
      fields: sectionFields,
    })),
    applier: { kind: 'host', available: true, reason: '' },
    managed: {
      config: { managed: false, source: null, at: null },
      secrets: { managed: false, source: null, at: null },
      admins: { managed: false, source: null, at: null },
    },
    document_digest: 'digest-1',
    started_at: '2026-10-12T08:00:00Z',
    problems: [],
    has_comments: false,
    secrets: [],
    admins: { users: ['alice'], read_only_reason: null },
    ...overrides,
  };
}

export function configPreview(overrides: Partial<ConfigPreview> = {}): ConfigPreview {
  return {
    diff: ['--- config.toml', '+++ config.toml', '-imap_host = "old"', '+imap_host = "new"'],
    problems: { blocking: [], warnings: [] },
    errors: [],
    changed: true,
    changed_sections: ['email'],
    changed_keys: 1,
    risky_keys: [],
    secret_names: [],
    admins_changed: false,
    running_tasks: 0,
    base_digest: 'digest-1',
    ...overrides,
  };
}

export function configRequest(overrides: Partial<ConfigRequest> = {}): ConfigRequest {
  return {
    id: 17,
    status: 'pending',
    admin_id: 'alice',
    base_digest: 'digest-1',
    document_toml: '',
    admins: null,
    admins_changed: false,
    changed_sections: ['email'],
    secret_names: [],
    diff_text: '',
    output_text: null,
    created_at: '2026-10-12T08:05:00Z',
    taken_at: null,
    finished_at: null,
    ...overrides,
  };
}
