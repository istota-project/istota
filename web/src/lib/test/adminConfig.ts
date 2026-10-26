/**
 * Test builders for the config editor's view payload: one field with every
 * key the server sends, and a view around a list of them. Overrides are
 * partial so a test states only what it is about.
 */
import type {
  AdminConfigField,
  AdminConfigView,
  ConfigCatalogueModel,
  ConfigModelCatalogue,
  ConfigModelField,
  ConfigPreview,
  ConfigRequest,
  ConfigSecretFile,
} from '$lib/api';

/** One entry of the view's `secrets`: a file credential with a config key,
 *  not set, unless the overrides say otherwise. */
export function configSecret(overrides: Partial<ConfigSecretFile> = {}): ConfigSecretFile {
  const configKey =
    overrides.config_key === undefined ? 'email.imap_password' : overrides.config_key;
  return {
    name: 'istota_email_imap_password',
    label: configKey ?? 'istota_email_imap_password',
    config_key: configKey,
    set: false,
    source: 'none',
    read_only_reason: null,
    movable: overrides.source === 'inline' && !overrides.read_only_reason,
    risky: false,
    ...overrides,
  };
}

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
    raw_toml: '',
    secrets: [],
    admins: { users: ['alice'], candidates: ['alice', 'bob'], read_only_reason: null },
    ...overrides,
  };
}

/** A catalogue with two Anthropic models (one deprecated), two OpenRouter
 *  models (one enabled) and the tiers plus one shortcut. */
export function configCatalogue(
  overrides: Partial<ConfigModelCatalogue> = {},
): ConfigModelCatalogue {
  const model = (ref: string, extra: Partial<ConfigCatalogueModel> = {}): ConfigCatalogueModel => ({
    ref,
    name: ref.split('/').slice(1).join('/'),
    family: null,
    deprecated: false,
    reasoning: true,
    efforts: ['low', 'medium', 'high'],
    context_window: 200000,
    enabled: false,
    ...extra,
  });
  return {
    sources: [
      {
        id: 'anthropic',
        kind: 'anthropic',
        label: 'Anthropic',
        discovered_at: null,
        last_error: null,
        models: [
          model('anthropic/claude-sonnet-5-5', {
            name: 'Claude Sonnet 5.5',
            family: 'sonnet',
            enabled: true,
            efforts: ['low', 'medium', 'high', 'xhigh', 'max'],
          }),
          model('anthropic/claude-sonnet-4-5', { family: 'sonnet', deprecated: true }),
        ],
      },
      {
        id: 'openrouter',
        kind: 'openrouter',
        label: 'OpenRouter',
        discovered_at: '2026-10-25T08:00:00Z',
        last_error: null,
        models: [
          model('openrouter/z-ai/glm-5.3-flash', { enabled: true, efforts: [], reasoning: false }),
          model('openrouter/moonshotai/kimi-k2.6', { name: 'Kimi K2.6' }),
        ],
      },
    ],
    aliases: [
      { name: 'fast', target: 'anthropic/claude-haiku-5-5', effort: null, efforts: ['low'] },
      { name: 'general', target: 'anthropic/claude-sonnet-5-5', effort: null, efforts: null },
      { name: 'smart', target: 'anthropic/claude-opus-5-5', effort: 'high', efforts: null },
      { name: 'opus', target: 'anthropic/claude-opus-5-5', effort: null, efforts: null },
    ],
    pattern_counts: {},
    ...overrides,
  };
}

/** A `model` field for `path`, naming `sources`. */
export function configModelField(
  path: string[],
  value: string,
  modelField: Partial<ConfigModelField> = {},
  overrides: Partial<AdminConfigField> = {},
): AdminConfigField {
  return configField({
    path,
    value,
    default: '',
    in_file: !!value,
    editor: 'model',
    model_field: { sources: ['anthropic'], effort: true, aliases: true, ...modelField },
    ...overrides,
  });
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
    moves: [],
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
    moves: [],
    diff_text: '',
    output_text: null,
    created_at: '2026-10-12T08:05:00Z',
    taken_at: null,
    finished_at: null,
    ...overrides,
  };
}
