/**
 * `@name` mentions on a transcript row (ISSUE-578).
 *
 * A user row is shown verbatim rather than through the markdown renderer, so
 * it has its own splitter; both have to produce the same `.mention` span. A
 * turn mirrored in from outside the room is left plain, and a row given no
 * list (the aggregate views) styles nothing.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

afterEach(cleanup);

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };
const mentions = [{ name: 'alice', self: true }, { name: 'bob' }, { name: 'Istota' }];

function msg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'user',
    text: 'thanks @bob, and @alice see `@bob`',
    segments: [],
    streaming: false,
    msgId: 7,
    ...over,
  };
}

const spans = (c: HTMLElement) =>
  [...c.querySelectorAll<HTMLElement>('.mention')].map((el) => ({
    text: el.textContent,
    self: el.classList.contains('mention-self'),
  }));

describe('mentions on a user row', () => {
  it('styles listed names and keeps every character of the text', () => {
    const { container } = render(Message, { ...base, message: msg(), mentions });
    expect(spans(container)).toEqual([
      { text: '@bob', self: false },
      { text: '@alice', self: true },
      // The backticked one stays plain: the row is verbatim, but a code span
      // is still code to its reader, as it is in a rendered row.
    ]);
    expect(container.querySelector('.user-text')?.textContent).toBe(
      'thanks @bob, and @alice see `@bob`',
    );
  });

  it('styles nothing without a list', () => {
    const { container } = render(Message, { ...base, message: msg() });
    expect(container.querySelector('.mention')).toBeNull();
    expect(container.querySelector('.user-text')?.textContent).toBe(msg().text);
  });

  it('leaves a turn mirrored in from outside the room plain', () => {
    const { container } = render(Message, {
      ...base,
      message: msg({ origin: 'email', author: 'someone@example.com' }),
      mentions,
      externalDisplay: 'full',
    });
    expect(container.querySelector('.user-text')?.textContent).toContain('@bob');
    expect(container.querySelector('.mention')).toBeNull();
  });
});

describe('mentions on a rendered row', () => {
  it('goes through the markdown renderer with the same list', () => {
    const { container } = render(Message, {
      ...base,
      message: msg({ role: 'system', text: 'ping @Istota and `@bob`' }),
      mentions,
    });
    expect(spans(container)).toEqual([{ text: '@Istota', self: false }]);
  });
});
