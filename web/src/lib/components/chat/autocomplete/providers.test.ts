import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the API module the providers pull the catalogue from.
vi.mock('$lib/api', () => ({
  fetchChatCommands: vi.fn(),
}));

import { fetchChatCommands } from '$lib/api';
import { commandProvider, modelAliasProvider, resetCommandCatalogue } from './providers';

const CATALOGUE = {
  commands: [
    { name: 'help', help: 'List available commands' },
    { name: 'memory', help: 'Show memory' },
    { name: 'models', help: 'List model aliases' },
    { name: 'more', help: 'Show execution trace' },
    { name: 'stop', help: 'Cancel your task' },
  ],
  model_aliases: [
    { alias: 'smart', target: 'claude-opus-4-8', effort: null },
    { alias: 'opus', target: 'claude-opus-4-8', effort: null },
    { alias: 'opus-high', target: 'claude-opus-4-8', effort: 'high' },
    { alias: 'sonnet', target: 'claude-sonnet-4-6', effort: null },
  ],
};

beforeEach(() => {
  resetCommandCatalogue();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockReset();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue(CATALOGUE);
});

describe('commandProvider.match', () => {
  const p = commandProvider();

  it('matches a bare ! at the start (empty query, full-token range)', () => {
    expect(p.match('!', 1)).toEqual({ query: '', range: [0, 1] });
  });

  it('matches a partial command name', () => {
    expect(p.match('!mo', 3)).toEqual({ query: 'mo', range: [0, 3] });
  });

  it('range covers the whole token when the caret is mid-token', () => {
    // "!mo|re" — caret at 3, tail "re" still in range so accept replaces it.
    expect(p.match('!more', 3)).toEqual({ query: 'mo', range: [0, 5] });
  });

  it('does not match once a space follows the command name', () => {
    expect(p.match('!more ', 6)).toBeNull();
  });

  it('does not match a ! mid-message', () => {
    expect(p.match('hi!', 3)).toBeNull();
    expect(p.match('hello world', 11)).toBeNull();
  });
});

describe('commandProvider.getSuggestions', () => {
  it('empty query returns all commands, prefix-then-substring ordered', async () => {
    const list = await commandProvider().getSuggestions('');
    expect(list.map((s) => s.label)).toEqual(['!help', '!memory', '!models', '!more', '!stop']);
    expect(list[0]).toMatchObject({
      value: '!help ',
      label: '!help',
      description: 'List available commands',
      key: 'cmd:help',
    });
  });

  it('prefix matches rank above substring matches', async () => {
    // query "mo": "models"/"more" prefix-match and rank first; "memory"
    // contains "mo" (me·mo·ry) so it follows as a substring match.
    const list = await commandProvider().getSuggestions('mo');
    expect(list.map((s) => s.label)).toEqual(['!models', '!more', '!memory']);
  });

  it('is case-insensitive', async () => {
    const list = await commandProvider().getSuggestions('MO');
    expect(list.map((s) => s.label)).toEqual(['!models', '!more', '!memory']);
  });

  it('caches the catalogue (one fetch across calls)', async () => {
    const p = commandProvider();
    await p.getSuggestions('');
    await p.getSuggestions('mo');
    expect(fetchChatCommands).toHaveBeenCalledTimes(1);
  });

  it('degrades to [] when the fetch fails', async () => {
    (fetchChatCommands as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('nope'));
    const list = await commandProvider().getSuggestions('');
    expect(list).toEqual([]);
  });
});

describe('modelAliasProvider', () => {
  const p = modelAliasProvider();

  it('matches only after "!model " while typing the alias', () => {
    expect(p.match('!model ', 7)).toEqual({ query: '', range: [7, 7] });
    expect(p.match('!model op', 9)).toEqual({ query: 'op', range: [7, 9] });
  });

  it('does not match a bare ! or !model without a space', () => {
    expect(p.match('!mo', 3)).toBeNull();
    expect(p.match('!model', 6)).toBeNull();
  });

  it('suggests aliases filtered by the query, canonical model as description', async () => {
    const list = await modelAliasProvider().getSuggestions('op');
    expect(list.map((s) => s.label)).toEqual(['opus', 'opus-high']);
    expect(list[0]).toMatchObject({
      value: 'opus ',
      label: 'opus',
      description: '(claude-opus-4-8)',
      key: 'model:opus',
    });
    // Effort-bearing alias carries the effort in the parens too.
    expect(list[1]).toMatchObject({
      label: 'opus-high',
      description: '(claude-opus-4-8 · high)',
    });
  });
});
