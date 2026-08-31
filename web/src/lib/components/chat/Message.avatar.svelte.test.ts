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

  it('gives a co-member their own picture, asked for without a version', () => {
    // The third state of `version`: nobody told the client this face's hash,
    // so the request goes out bare and pays one conditional round trip per
    // author per session rather than a field on every streamed row (D13).
    const { container } = render(Message, {
      message: turn({ role: 'user', text: 'hello', author: 'Bob', authorId: 'bob' }),
      onConfirm: noop,
      onReject: noop,
      userName: 'Alice',
      userId: 'alice',
      userAvatar: 'me77',
    });

    // Not `alice`, and not `?v=me77`: the viewer's own hash on someone else's
    // id is the wrong person's face under an immutable cache entry.
    expect(gutterImage(container)?.getAttribute('src')).toBe('/api/avatars/user/bob');
  });

  it("leaves an external sender's turn on the chip, since they have no id", () => {
    // A mirrored email carries a display label and no `author_id` — the sender
    // is not an istota user, so there is nothing to ask the endpoint for. The
    // viewer's own id must not stand in for the missing one.
    const { container } = render(Message, {
      message: turn({ role: 'user', text: 'hello', author: 'contact@example.com' }),
      onConfirm: noop,
      onReject: noop,
      userName: 'Alice',
      userId: 'alice',
      userAvatar: 'me77',
    });

    expect(gutterImage(container)).toBeNull();
    expect(gutterChip(container)?.textContent?.trim()).toBe('C');
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
    // Comments out, the carve-out `Avatar.svelte.test.ts` makes for the same
    // class of assertion: the rules that left are worth explaining where they
    // used to be, and prose naming the selector it forbids would turn this red
    // for saying so.
    const css = styleBlock.replace(/\/\*[\s\S]*?\*\//g, '');

    expect(css).not.toMatch(/\.avatar\b/);
  });
});
