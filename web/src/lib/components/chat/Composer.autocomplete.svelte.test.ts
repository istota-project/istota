import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

// Mock the API: attachment upload (unused here) + the command catalogue.
vi.mock('$lib/api', () => ({
  uploadChatAttachment: vi.fn(),
  fetchChatCommands: vi.fn(),
}));

import { fetchChatCommands } from '$lib/api';
import { resetCommandCatalogue } from './autocomplete/providers';
import Composer from './Composer.svelte';

const CATALOGUE = {
  commands: [
    { name: 'memory', help: 'Show memory' },
    { name: 'models', help: 'List model aliases' },
    { name: 'more', help: 'Show execution trace' },
    { name: 'stop', help: 'Cancel your task' },
  ],
  model_aliases: [{ alias: 'opus', target: 'claude-opus-4-8', effort: null }],
};

afterEach(cleanup);
beforeEach(() => {
  resetCommandCatalogue();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockReset();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue(CATALOGUE);
});

function mount(onSend = vi.fn()) {
  const utils = render(Composer, { onSend });
  const textarea = utils.container.querySelector('textarea') as HTMLTextAreaElement;
  return { ...utils, textarea, onSend };
}

/** Simulate typing a value into the textarea (updates value + fires input). */
async function type(textarea: HTMLTextAreaElement, value: string) {
  textarea.value = value;
  textarea.selectionStart = textarea.selectionEnd = value.length;
  await fireEvent.input(textarea);
  await tick();
  // getSuggestions is async; let the microtask queue drain.
  await Promise.resolve();
  await tick();
}

function key(textarea: HTMLTextAreaElement, k: string, opts: KeyboardEventInit = {}) {
  return fireEvent.keyDown(textarea, { key: k, ...opts });
}

describe('Composer autocomplete', () => {
  it('typing ! opens the command listbox with all commands', async () => {
    const { container, textarea } = mount();
    await type(textarea, '!');
    const list = container.querySelector('[role="listbox"]');
    expect(list).toBeTruthy();
    const opts = container.querySelectorAll('[role="option"]');
    expect(opts.length).toBe(4);
    expect(textarea.getAttribute('aria-expanded')).toBe('true');
  });

  it('filters as more characters are typed', async () => {
    const { container, textarea } = mount();
    await type(textarea, '!mo');
    const labels = [...container.querySelectorAll('[role="option"] .ac-label')].map(
      (e) => e.textContent,
    );
    // prefix: models, more; substring: memory (me·mo·ry). "stop" excluded.
    expect(labels).toEqual(['!models', '!more', '!memory']);
  });

  it('ArrowDown + Enter accepts the highlighted command and does not send', async () => {
    const { container, textarea, onSend } = mount();
    await type(textarea, '!m'); // rows: !memory, !models, !more
    key(textarea, 'ArrowDown');
    key(textarea, 'ArrowDown'); // → !more
    await tick();
    key(textarea, 'Enter'); // accept, must NOT send
    await tick();
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toBe('!more ');
    // Popover closed after accept.
    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });

  it('Tab accepts the highlighted command', async () => {
    const { textarea } = mount();
    await type(textarea, '!mo'); // first row = !models
    key(textarea, 'Tab');
    await tick();
    expect(textarea.value).toBe('!models ');
  });

  it('Escape closes the popover; the next Enter sends', async () => {
    const { container, textarea, onSend } = mount();
    await type(textarea, '!mo');
    expect(container.querySelector('[role="listbox"]')).toBeTruthy();
    key(textarea, 'Escape');
    await tick();
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    key(textarea, 'Enter');
    await tick();
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('!mo', []);
  });

  it('Enter sends normally when the popover is closed', async () => {
    const { textarea, onSend } = mount();
    await type(textarea, 'hello there');
    key(textarea, 'Enter');
    await tick();
    expect(onSend).toHaveBeenCalledWith('hello there', []);
  });

  it('clicking a row accepts it', async () => {
    const { container, textarea } = mount();
    await type(textarea, '!mo');
    const rows = container.querySelectorAll('[role="option"]');
    await fireEvent.mouseDown(rows[1]); // !more
    await tick();
    expect(textarea.value).toBe('!more ');
  });

  it('accepting !model chains into the alias list', async () => {
    const { container, textarea } = mount();
    await type(textarea, '!model'); // only "models" (command) here; type the space
    // Directly type the model prefix + trailing space to trigger the alias provider.
    await type(textarea, '!model ');
    const labels = [...container.querySelectorAll('[role="option"] .ac-label')].map(
      (e) => e.textContent,
    );
    expect(labels).toEqual(['opus']);
  });
});
