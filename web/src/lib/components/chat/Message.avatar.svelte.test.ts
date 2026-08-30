import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { ChatMessage } from '$lib/stores/chat';
import Message from './Message.svelte';

afterEach(cleanup);

const noop = () => {};

function turn(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'assistant',
    text: 'the answer',
    streaming: false,
    segments: [{ kind: 'text', id: 's1', text: 'the answer', settled: false }],
    createdAt: '2026-07-10T12:00:00Z',
    ...over,
  };
}

const gutterImage = (c: HTMLElement) => c.querySelector('.gutter img');
const gutterChip = (c: HTMLElement) => c.querySelector('.gutter .fallback');

describe('the chat gutter renders an identity, not a letter', () => {
  it('gives the bot its icon when the deployment has one', () => {
    const { container } = render(Message, {
      message: turn(),
      onConfirm: noop,
      onReject: noop,
      botName: 'Istota',
      botAvatar: 'bot99',
    });

    expect(gutterImage(container)?.getAttribute('src')).toBe('/api/avatars/bot?v=bot99');
  });

  it('gives the viewer their own picture on their own turn', () => {
    const { container } = render(Message, {
      message: turn({ role: 'user', text: 'the question' }),
      onConfirm: noop,
      onReject: noop,
      userName: 'Alice',
      userId: 'alice',
      userAvatar: 'me77',
    });

    expect(gutterImage(container)?.getAttribute('src')).toBe('/api/avatars/user/alice?v=me77');
  });

  it('renders the initial chip and asks for nothing when no picture is set', () => {
    // The default on every prop, so a caller that knows nothing about avatars
    // — every aggregate pane, and the harness — renders exactly what it did.
    const { container } = render(Message, {
      message: turn(),
      onConfirm: noop,
      onReject: noop,
      botName: 'Istota',
    });

    expect(gutterImage(container)).toBeNull();
    expect(gutterChip(container)?.textContent?.trim()).toBe('I');
  });

  it("leaves another member's turn on the chip, since the client has no id for them", () => {
    // `author_id` on the message row is Stage 6. Until it lands, a turn the
    // server named someone else for must not be drawn with the *viewer's* own
    // picture, which is what passing the id through unconditionally would do.
    const { container } = render(Message, {
      message: turn({ role: 'user', text: 'hello', author: 'Bob' }),
      onConfirm: noop,
      onReject: noop,
      userName: 'Alice',
      userId: 'alice',
      userAvatar: 'me77',
    });

    expect(gutterImage(container)).toBeNull();
    expect(gutterChip(container)?.textContent?.trim()).toBe('B');
  });

  it('collapses the whole identity on a continuation row', () => {
    const { container } = render(Message, {
      message: turn(),
      continuation: true,
      onConfirm: noop,
      onReject: noop,
      botName: 'Istota',
      botAvatar: 'bot99',
    });

    expect(gutterImage(container)).toBeNull();
    expect(gutterChip(container)).toBeNull();
  });
});

describe('the avatar rules left Message.svelte', () => {
  it('has no .avatar selector of its own', () => {
    // Svelte scopes styles per component, so a `.avatar` rule left behind here
    // matches nothing and `svelte-check` reports it as unused rather than as
    // broken. The primitive owns the box, the chip fill and the mobile step.
    const source = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), 'Message.svelte'),
      'utf8',
    );
    const styleOpen = source.indexOf('>', source.indexOf('<style'));
    const styleBlock = source.slice(styleOpen + 1, source.lastIndexOf('</style>'));

    expect(styleBlock).not.toMatch(/\.avatar\b/);
  });
});
