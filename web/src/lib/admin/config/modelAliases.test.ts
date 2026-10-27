import { describe, expect, it } from 'vitest';
import { aliasesDraft, aliasesForm, aliasesToml, withAlias, withTarget } from './modelAliases';

describe('aliasesForm', () => {
  it('has a row per tier even when the file overrides none', () => {
    expect(aliasesForm({}).map((row) => row.name)).toEqual(['fast', 'general', 'smart']);
  });

  it('reads each namespace’s target, flat strings and the portable flag', () => {
    const form = aliasesForm({
      fast: 'haiku:low',
      house: {
        anthropic: 'opus:high',
        openai_compat: { model: 'openrouter/z-ai/glm-5.3-flash', effort: 'medium' },
        portable: true,
      },
      smart: { anthropic: { effort: 'max' } },
    });
    const fast = form.find((r) => r.name === 'fast')!;
    expect(fast.flat).toBe(true);
    expect(fast.targets.anthropic).toEqual({ model: 'haiku', effort: 'low' });
    const house = form.find((r) => r.name === 'house')!;
    expect(house.portable).toBe(true);
    expect(house.targets.anthropic).toEqual({ model: 'opus', effort: 'high' });
    expect(house.targets.openai_compat).toEqual({
      model: 'openrouter/z-ai/glm-5.3-flash',
      effort: 'medium',
    });
    expect(form.find((r) => r.name === 'smart')!.targets.anthropic).toEqual({
      model: '',
      effort: 'max',
    });
  });
});

describe('aliasesToml', () => {
  it('writes the table back as one snippet whose key is the field', () => {
    const form = aliasesForm({
      fast: 'haiku:low',
      house: { anthropic: 'opus', openai_compat: { model: 'x/y', effort: 'high' }, portable: true },
      smart: { anthropic: { effort: 'max' } },
    });
    expect(aliasesToml(form)).toBe(
      [
        '[aliases]',
        'fast = "haiku:low"',
        'smart = { anthropic = { effort = "max" } }',
        'house = { anthropic = "opus", openai_compat = { model = "x/y", effort = "high" }, portable = true }',
        '',
      ].join('\n'),
    );
  });

  it('leaves out an alias with no target, which returns it to its default', () => {
    expect(aliasesToml(aliasesForm({}))).toBe('[aliases]\n');
  });
});

describe('aliasesDraft', () => {
  it('is empty until something changes', () => {
    const initial = aliasesForm({ fast: 'haiku' });
    expect(aliasesDraft(aliasesForm({ fast: 'haiku' }), initial).ops).toEqual({});
  });

  it('is one set_toml op on models.aliases', () => {
    const initial = aliasesForm({});
    let form = withTarget(initial, 'general', 'openai_compat', {
      model: 'openrouter/moonshotai/kimi-k2.6',
      effort: 'high',
    });
    form = withAlias(form, 'Writer');
    form = withTarget(form, 'writer', 'anthropic', { model: 'sonnet', effort: '' });
    const ops = Object.values(aliasesDraft(form, initial).ops);
    expect(ops).toEqual([
      {
        op: 'set_toml',
        path: ['models', 'aliases'],
        toml:
          '[aliases]\n' +
          'general = { openai_compat = { model = "openrouter/moonshotai/kimi-k2.6", effort = "high" } }\n' +
          'writer = { anthropic = "sonnet" }\n',
      },
    ]);
  });

  it('refuses a name that is taken or not an alias name', () => {
    const form = aliasesForm({});
    expect(withAlias(form, 'fast')).toBe(form);
    expect(withAlias(form, 'two words')).toBe(form);
  });

  it('splits a flat alias when one namespace is edited', () => {
    const form = withTarget(aliasesForm({ fast: 'haiku' }), 'fast', 'openai_compat', {
      model: 'x/y',
      effort: '',
    });
    expect(aliasesToml(form)).toContain('fast = { anthropic = "haiku", openai_compat = "x/y" }');
  });
});
