import { describe, expect, it } from 'vitest';
import { configCatalogue } from '$lib/test/adminConfig';
import {
  ALIASES,
  OTHER_LABEL,
  joinEffort,
  modelOptions,
  otherValue,
  parseStored,
  patternCount,
  sourceChoices,
  splitEffort,
} from './modelField';

const catalogue = configCatalogue();
const anthropicOnly = { sources: ['anthropic'], effort: true, aliases: true };
const both = { sources: ['openrouter', 'anthropic'], effort: true, aliases: true };

describe('splitEffort', () => {
  it('splits a known level only', () => {
    expect(splitEffort('smart:high')).toEqual({ base: 'smart', effort: 'high' });
    expect(splitEffort('openrouter/deepseek/r1:free')).toEqual({
      base: 'openrouter/deepseek/r1:free',
      effort: '',
    });
    expect(joinEffort('opus', 'max')).toBe('opus:max');
    expect(joinEffort('  ', 'max')).toBe('');
  });
});

describe('sourceChoices', () => {
  it('lists Aliases, then the field’s providers in its order', () => {
    expect(sourceChoices(catalogue, both)).toEqual([
      { value: ALIASES, label: 'Aliases' },
      { value: 'openrouter', label: 'OpenRouter' },
      { value: 'anthropic', label: 'Anthropic' },
    ]);
    expect(sourceChoices(catalogue, { ...anthropicOnly, aliases: false })).toEqual([
      { value: 'anthropic', label: 'Anthropic' },
    ]);
  });
});

describe('parseStored', () => {
  it('puts an alias under Aliases with its effort', () => {
    expect(parseStored('smart:high', catalogue, anthropicOnly)).toEqual({
      source: ALIASES,
      model: 'smart',
      effort: 'high',
      known: true,
    });
  });

  it('puts a ref under its provider', () => {
    expect(parseStored('openrouter/moonshotai/kimi-k2.6', catalogue, both)).toMatchObject({
      source: 'openrouter',
      known: true,
    });
  });

  it('shows a bare id one provider lists as its ref', () => {
    expect(parseStored('claude-sonnet-5-5', catalogue, anthropicOnly)).toMatchObject({
      source: 'anthropic',
      model: 'anthropic/claude-sonnet-5-5',
      known: true,
    });
  });

  it('keeps a value the catalogue does not hold as it is, unknown', () => {
    expect(parseStored('claude-legacy-20240101', catalogue, anthropicOnly)).toEqual({
      source: 'anthropic',
      model: 'claude-legacy-20240101',
      effort: '',
      known: false,
    });
    expect(parseStored('anthropic/claude-gone', catalogue, anthropicOnly).known).toBe(false);
  });
});

describe('modelOptions', () => {
  it('ends a provider’s list with Other…, marking enabled and deprecated models', () => {
    const labels = modelOptions(catalogue, 'anthropic').map((o) => o.label);
    expect(labels).toEqual([
      'anthropic/claude-sonnet-5-5 (Claude Sonnet 5.5) · enabled',
      'anthropic/claude-sonnet-4-5 · deprecated',
      OTHER_LABEL,
    ]);
  });

  it('lists the aliases with their targets', () => {
    expect(modelOptions(catalogue, ALIASES)[0]).toEqual({
      label: 'fast → anthropic/claude-haiku-5-5',
      value: 'fast',
      efforts: ['low'],
    });
  });

  it('saves a typed id under the chosen provider', () => {
    expect(otherValue('openrouter', 'new/model')).toBe('openrouter/new/model');
    expect(otherValue('openrouter', 'openrouter/new/model')).toBe('openrouter/new/model');
    expect(otherValue('openrouter', '  ')).toBe('');
  });
});

describe('patternCount', () => {
  it('counts by the server’s rule', () => {
    expect(patternCount(catalogue, 'anthropic/*')).toBe(1);
    expect(patternCount(catalogue, 'ANTHROPIC/claude-sonnet-4-5')).toBe(1);
    expect(patternCount(catalogue, 'openrouter/*:high')).toBe(2);
    expect(patternCount(catalogue, 'openrouter/z-ai/glm-5.?-flash')).toBe(1);
    expect(patternCount(catalogue, 'nosuch/*')).toBe(0);
    expect(patternCount(catalogue, '')).toBe(0);
  });
});
