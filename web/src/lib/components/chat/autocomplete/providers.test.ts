import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the API module the providers pull the catalogue from.
vi.mock('$lib/api', () => ({
  fetchChatCommands: vi.fn(),
}));

import { fetchChatCommands } from '$lib/api';
import {
  commandProvider,
  getBaseModelChoices,
  isKnownCommand,
  loadCommandNames,
  modelAliasProvider,
  resetCommandCatalogue,
} from './providers';

const CATALOGUE = {
  commands: [
    { name: 'help', help: 'List available commands' },
    { name: 'memory', help: 'Show memory' },
    { name: 'models', help: 'List model aliases' },
    { name: 'more', help: 'Show execution trace' },
    { name: 'stop', help: 'Cancel your task' },
  ],
  model_aliases: [
    // Base names only — effort is the orthogonal :effort modifier, never a
    // separate alias row (centralized-model-alias-registry spec).
    { alias: 'smart', target: 'claude-opus-4-8', effort: null },
    { alias: 'opus', target: 'claude-opus-4-8', effort: null },
    { alias: 'sonnet', target: 'claude-sonnet-5', effort: null },
    { alias: 'haiku', target: 'claude-haiku-4-5', effort: null },
  ],
  command_aliases: [
    { alias: 'inject', target: 'steer' },
    { alias: 'yes', target: 'confirm' },
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
    // Base names only — 'op' prefix-matches just 'opus' now.
    expect(list.map((s) => s.label)).toEqual(['opus']);
    expect(list[0]).toMatchObject({
      value: 'opus ',
      label: 'opus',
      description: '(claude-opus-4-8)',
      key: 'model:opus',
    });
  });

  it('carries an alias-resolved effort in the description when present', async () => {
    // A custom operator alias may resolve to (model, effort); the parens show it.
    (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      commands: CATALOGUE.commands,
      model_aliases: [{ alias: 'deep', target: 'claude-opus-4-8', effort: 'max' }],
    });
    resetCommandCatalogue();
    const list = await modelAliasProvider().getSuggestions('deep');
    expect(list[0]).toMatchObject({
      label: 'deep',
      description: '(claude-opus-4-8 · max)',
    });
  });
});

describe('getBaseModelChoices', () => {
  it('one choice per distinct model, provider-shortcut label preferred', async () => {
    const choices = await getBaseModelChoices();
    // smart + opus both map to claude-opus-4-8 → deduped, labeled 'opus'.
    expect(choices).toEqual([
      { value: 'claude-opus-4-8', label: 'opus' },
      { value: 'claude-sonnet-5', label: 'sonnet' },
      { value: 'claude-haiku-4-5', label: 'haiku' },
    ]);
  });
});

describe('isKnownCommand', () => {
  it('recognises a registered command name', async () => {
    await loadCommandNames();
    expect(isKnownCommand('!stop')).toBe(true);
  });

  it('recognises a hidden alias, which dispatch resolves server-side (ISSUE-350)', async () => {
    await loadCommandNames();
    expect(isKnownCommand('!inject do the other thing')).toBe(true);
    expect(isKnownCommand('!yes')).toBe(true);
  });

  it('still refuses a name the server registers under neither', async () => {
    await loadCommandNames();
    expect(isKnownCommand('!hepl')).toBe(false);
    // `model` is deliberately not a command: `!model <alias> <prompt>` is a
    // prefix that creates a task, so it must keep taking the turn path.
    expect(isKnownCommand('!model opus write a poem')).toBe(false);
  });

  it('survives a malformed command_aliases rather than poisoning the catalogue', async () => {
    // The assignment runs in `loadCatalogue`'s `.then`, after its `.catch`, so
    // a throw here would reject the cached promise for the whole session and
    // take the `!` popover and the room-settings model dropdown down with it.
    (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...CATALOGUE,
      command_aliases: { nope: true },
    });
    await expect(loadCommandNames()).resolves.toBeDefined();
    expect(isKnownCommand('!stop')).toBe(true);
    expect(isKnownCommand('!inject')).toBe(false);
    // The other consumers of the same cached promise still resolve.
    await expect(commandProvider().getSuggestions('')).resolves.toBeDefined();
  });

  it('ignores a fetch that resolves after its own session was torn down', async () => {
    // Otherwise the stale assignment lands on top of the fresh one and the
    // routing set is empty for the rest of the session, queueing every
    // command — `!stop` included.
    let releaseStale: (v: unknown) => void = () => {};
    (fetchChatCommands as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((r) => {
        releaseStale = r;
      }),
    );
    const stale = loadCommandNames();

    resetCommandCatalogue();
    (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue(CATALOGUE);
    await loadCommandNames();
    expect(isKnownCommand('!stop')).toBe(true);

    releaseStale({ commands: [], model_aliases: [], command_aliases: [] });
    await stale;
    expect(isKnownCommand('!stop')).toBe(true);
  });

  it('keeps aliases out of the autocomplete suggestions', async () => {
    const sugg = await commandProvider().getSuggestions('');
    expect(sugg.map((s) => s.label)).not.toContain('!inject');
    expect(sugg.map((s) => s.label)).toContain('!stop');
  });
});
