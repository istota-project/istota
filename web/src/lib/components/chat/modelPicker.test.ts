import { describe, it, expect } from 'vitest';
import type { ChatModel } from '$lib/api';
import {
  defaultModelFor,
  effectiveProvider,
  effortOptions,
  effortsFor,
  filterOptions,
  keepsOnProvider,
  modelRefusalOf,
  pickerOptions,
  providerOptions,
  refusalMessage,
  valueOf,
  type ModelChoices,
} from './modelPicker';

function model(ref: string, overrides: Partial<ChatModel> = {}): ChatModel {
  const provider = ref.split('/')[0];
  return {
    ref,
    name: ref.slice(provider.length + 1),
    provider,
    family: null,
    reasoning: true,
    efforts: ['low', 'medium', 'high'],
    context_window: 200000,
    input: ['text'],
    price: null,
    enabled: true,
    ...overrides,
  };
}

function choices(overrides: Partial<ModelChoices> = {}): ModelChoices {
  return {
    aliases: [{ alias: 'smart', target: 'claude-opus-5-5', effort: null, efforts: ['low', 'max'] }],
    models: [
      model('anthropic/claude-opus-5-5', { name: 'Claude Opus 5.5' }),
      model('openrouter/moonshotai/kimi-k2.6'),
      model('openrouter/vendor/x', { enabled: false, efforts: [] }),
    ],
    defaultModel: null,
    roomModel: null,
    ...overrides,
  };
}

describe('pickerOptions', () => {
  it('lists the aliases first, then the models in the order sent', () => {
    const options = pickerOptions(choices());
    expect(options.map((o) => o.value)).toEqual([
      'smart',
      'anthropic/claude-opus-5-5',
      'openrouter/moonshotai/kimi-k2.6',
      'openrouter/vendor/x',
    ]);
    expect(options[0].label).toBe('smart → claude-opus-5-5');
    expect(options[1].label).toBe('anthropic/claude-opus-5-5 · enabled');
  });

  it("marks the enabled models only on a list that mixes them, an admin's", () => {
    const admin = pickerOptions(choices());
    expect(admin[2].label).toBe('openrouter/moonshotai/kimi-k2.6 · enabled');
    expect(admin[3].label).toBe('openrouter/vendor/x');
    const member = pickerOptions(
      choices({ models: [model('anthropic/claude-opus-5-5'), model('openrouter/m/n')] }),
    );
    expect(member.every((o) => !o.label.includes('enabled'))).toBe(true);
  });

  it("names the brain's own model for an alias with no target", () => {
    const options = pickerOptions(
      choices({ aliases: [{ alias: 'fast', target: null, effort: null }], models: [] }),
    );
    expect(options[0]).toEqual({
      label: "fast → the brain's own model",
      value: 'fast',
      efforts: null,
    });
  });
});

describe('valueOf', () => {
  const options = pickerOptions(choices());

  it('maps a picked label to what it stores', () => {
    expect(valueOf(options, 'openrouter/moonshotai/kimi-k2.6 · enabled')).toBe(
      'openrouter/moonshotai/kimi-k2.6',
    );
    expect(valueOf(options, 'smart → claude-opus-5-5')).toBe('smart');
  });

  it('keeps typed text as typed, trimmed, and empty as the default', () => {
    expect(valueOf(options, '  openrouter/other/model ')).toBe('openrouter/other/model');
    expect(valueOf(options, '')).toBe('');
  });
});

describe('filterOptions', () => {
  const options = pickerOptions(choices());
  const labels = options.map((o) => o.label);
  const filter = filterOptions(options);

  it('filters by substring of the label', () => {
    expect(filter(labels, 'kimi')).toEqual(['openrouter/moonshotai/kimi-k2.6 · enabled']);
  });

  it('shows the whole list for a field holding a picked value', () => {
    expect(filter(labels, 'anthropic/claude-opus-5-5')).toEqual(labels);
    expect(filter(labels, '')).toEqual(labels);
  });

  it("shows the whole list for the room's stored pin, listed or not", () => {
    expect(filterOptions(options, 'openrouter/gone/m')(labels, 'openrouter/gone/m')).toEqual(
      labels,
    );
    expect(filter(labels, 'openrouter/gone/m')).toEqual([]);
  });
});

describe('effortsFor and effortOptions', () => {
  const options = pickerOptions(choices());

  it("offers only the chosen model's levels", () => {
    const efforts = effortsFor(options, null, 'smart');
    expect(efforts).toEqual(['low', 'max']);
    expect(effortOptions(efforts, '').map((o) => o.value)).toEqual(['', 'low', 'max']);
  });

  it('offers no level for a model without reasoning', () => {
    const efforts = effortsFor(options, null, 'openrouter/vendor/x');
    expect(effortOptions(efforts, '').map((o) => o.value)).toEqual(['']);
  });

  it('offers every level for the default and for a model nobody knows', () => {
    expect(effortsFor(options, null, '')).toBeNull();
    expect(effortsFor(options, null, 'typed/model')).toBeNull();
    expect(effortOptions(null, '').map((o) => o.value)).toEqual([
      '',
      'low',
      'medium',
      'high',
      'xhigh',
      'max',
    ]);
  });

  it("reads a pin outside the list off the room's own status", () => {
    const roomModel = {
      value: 'openrouter/admin/pick',
      status: 'set_by_admin' as const,
      ref: 'openrouter/admin/pick',
      efforts: ['medium'],
    };
    expect(effortsFor(options, roomModel, 'openrouter/admin/pick')).toEqual(['medium']);
  });

  it('keeps a stored level the model does not take, marked', () => {
    const out = effortOptions(['low', 'medium', 'high'], 'max');
    expect(out[out.length - 1]).toEqual({
      value: 'max',
      label: 'max (this model takes up to high)',
    });
  });
});

describe('a refused pick', () => {
  it('reads the 422 body off a caught error', () => {
    const e = Object.assign(new Error('x'), {
      status: 422,
      body: { error: 'ambiguous', code: 'ambiguous', candidates: ['a/b', 'c/d', 7] },
    });
    expect(modelRefusalOf(e)).toEqual({
      error: 'ambiguous',
      code: 'ambiguous',
      candidates: ['a/b', 'c/d'],
    });
  });

  it('is not a refusal without a code, or on another status', () => {
    expect(modelRefusalOf(Object.assign(new Error('x'), { status: 422, body: {} }))).toBeNull();
    expect(
      modelRefusalOf(Object.assign(new Error('x'), { status: 403, body: { code: 'unknown' } })),
    ).toBeNull();
  });

  it('says what each code means', () => {
    const refusal = (code: string) =>
      refusalMessage({ error: 'server text', code, candidates: [] });
    expect(refusal('model_not_enabled')).toBe('That model is not one you can pick.');
    expect(refusal('model_not_runnable')).toBe("This room's brain cannot run that model.");
    expect(refusal('unknown')).toBe('server text');
    expect(refusal('something_new')).toBe('server text');
    expect(refusal('provider_admin_only')).toMatch(/only an admin can change the provider/);
    expect(refusal('no_default_model')).toMatch(/no default model/);
  });
});

describe("a native room's provider", () => {
  const PROVIDERS = [
    { id: 'anthropic', kind: 'anthropic', default: true, default_model: 'claude-sonnet-5-5' },
    { id: 'openrouter', kind: 'openrouter', default: false, default_model: null },
  ];

  it('offers the default provider first, as the empty value', () => {
    expect(providerOptions(choices({ providers: PROVIDERS }))).toEqual([
      { value: '', label: 'anthropic (default)' },
      { value: 'openrouter', label: 'openrouter' },
    ]);
    expect(providerOptions(choices())).toEqual([]);
  });

  it("lists only the chosen provider's models, aliases still first", () => {
    const options = pickerOptions(choices(), 'openrouter');
    expect(options.map((o) => o.value)).toEqual([
      'smart',
      'openrouter/moonshotai/kimi-k2.6',
      'openrouter/vendor/x',
    ]);
  });

  it('runs on the default provider until another is chosen', () => {
    const c = choices({ providers: PROVIDERS });
    expect(effectiveProvider(c, '')).toBe('anthropic');
    expect(effectiveProvider(c, 'openrouter')).toBe('openrouter');
  });

  it("names the provider's own default model for a provider being moved to", () => {
    const c = choices({ providers: PROVIDERS, defaultModel: 'the-room-default' });
    expect(defaultModelFor(c, '', '')).toBe('the-room-default');
    expect(defaultModelFor(c, 'openrouter', '')).toBeNull();
    expect(defaultModelFor(c, '', 'openrouter')).toBe('claude-sonnet-5-5');
  });

  it("keeps an alias and the new provider's own models, nothing else", () => {
    const options = pickerOptions(choices());
    expect(keepsOnProvider(options, 'smart', 'openrouter')).toBe(true);
    expect(keepsOnProvider(options, 'openrouter/vendor/x', 'openrouter')).toBe(true);
    expect(keepsOnProvider(options, 'anthropic/claude-opus-5-5', 'openrouter')).toBe(false);
    expect(keepsOnProvider(options, 'qwen3', 'openrouter')).toBe(false);
    expect(keepsOnProvider(options, '', 'openrouter')).toBe(true);
  });
});
