/**
 * Reply affordance and citation rendering on a chat message.
 *
 * Two halves: the button that stages a reply (offered only where a staged
 * reply has somewhere to go), and the quote block a turn carrying a citation
 * renders above its body — clickable for a live parent, muted and inert for a
 * deleted one.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import type { ChatMessage } from '$lib/stores/segments';
import { clearNotices } from '$lib/stores/notices';
import Message from './Message.svelte';

const source = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), 'Message.svelte'),
  'utf8',
);

afterEach(() => {
  cleanup();
  clearNotices();
  vi.restoreAllMocks();
});

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

function userMsg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'user',
    text: 'about that',
    segments: [],
    streaming: false,
    msgId: 41,
    ...over,
  };
}

function replyButton(container: HTMLElement): HTMLButtonElement | null {
  return container.querySelector<HTMLButtonElement>('.turn-action[aria-label="Reply to message"]');
}

describe('reply button', () => {
  it('is offered on a durable row when a handler is passed', () => {
    const { container } = render(Message, { ...base, message: userMsg(), onReply: noop });
    expect(replyButton(container)).not.toBeNull();
  });

  it('is withheld without a handler', () => {
    const { container } = render(Message, { ...base, message: userMsg() });
    expect(replyButton(container)).toBeNull();
  });

  it('is withheld on a row with no durable id', () => {
    // An optimistic row, an in-flight placeholder — nothing a citation could
    // name yet. Same rule star and delete already follow.
    const { container } = render(Message, {
      ...base,
      message: userMsg({ msgId: undefined }),
      onReply: noop,
    });
    expect(replyButton(container)).toBeNull();
  });

  it('is withheld in an aggregate view', () => {
    // All / Starred have no composer, so a staged reply has nowhere to go.
    const { container } = render(Message, {
      ...base,
      message: userMsg(),
      onReply: noop,
      aggregate: true,
    });
    expect(replyButton(container)).toBeNull();
  });

  it('is withheld on a failed send', () => {
    const { container } = render(Message, {
      ...base,
      message: userMsg({ sendState: 'failed', sendError: 'nope' }),
      onReply: noop,
    });
    expect(replyButton(container)).toBeNull();
  });

  it('hands the cid to the handler', async () => {
    const onReply = vi.fn();
    const { container } = render(Message, {
      ...base,
      message: userMsg({ cid: 7 }),
      onReply,
    });
    await fireEvent.click(replyButton(container)!);
    expect(onReply).toHaveBeenCalledWith(7);
  });

  it('ends the row after copy and star but before delete', () => {
    // Ascending consequence: reply stages a new message — more than a private
    // mark, less than a destructive removal.
    const { container } = render(Message, {
      ...base,
      message: userMsg(),
      onReply: noop,
      onToggleStar: noop,
      onDelete: noop,
    });
    const labels = Array.from(
      container.querySelectorAll<HTMLButtonElement>('.turn-actions .turn-action'),
    ).map((b) => b.getAttribute('aria-label'));
    expect(labels).toEqual(['Copy message', 'Star message', 'Reply to message', 'Delete message']);
  });
});

describe('touch targets with a fourth button', () => {
  it('keeps the overlay width derived from the button pitch', () => {
    // The row went from three buttons to four, so the targets got tighter.
    // They still tile exactly rather than overlapping *because* the width is
    // the button plus one gap — at a flat 44px each, four of them would need
    // ~21px between icons to stay apart and would instead overlap, handing a
    // tap in the seam to whichever won the stacking order. Read from the
    // source, since jsdom applies no CSS: the rule is the artefact under test.
    //
    // The arithmetic at the default text scale (root 17.6px): a button is the
    // 15px icon plus 2 × --space-1 = 23.8px, the gap below 768px is --space-2
    // = 8.8px, so four of them span 4 × 23.8 + 3 × 8.8 ≈ 122px and each
    // overlay is 32.6 × 44. On a 375px phone the transcript's content column
    // is ~322px (375 minus the 2.25rem gutter sum and the trailing inset), so
    // the row takes about a third of the narrowest bubble; at the large text
    // scale it is ~127px against ~317px. Comfortable either way.
    expect(source).toContain('width: calc(100% + var(--turn-action-gap));');
    expect(source).toContain('--turn-action-gap: var(--space-2);');
  });
});

describe('citation quote block', () => {
  it('renders the parent excerpt above the body', () => {
    const { container } = render(Message, {
      ...base,
      message: userMsg({
        replyTo: { msgId: 22, role: 'assistant', excerpt: 'the earlier answer' },
      }),
    });
    const quote = container.querySelector('.reply-quote');
    expect(quote?.textContent).toContain('the earlier answer');
  });

  it('clicks through to the cited message', async () => {
    const onJumpToMessage = vi.fn();
    const { container } = render(Message, {
      ...base,
      message: userMsg({
        replyTo: { msgId: 22, role: 'assistant', excerpt: 'the earlier answer' },
      }),
      onJumpToMessage,
    });
    await fireEvent.click(container.querySelector<HTMLElement>('.reply-quote')!);
    expect(onJumpToMessage).toHaveBeenCalledWith(22);
  });

  it('renders a deleted parent muted and inert', () => {
    const { container } = render(Message, {
      ...base,
      message: userMsg({ replyTo: { msgId: 22, deleted: true } }),
      onJumpToMessage: vi.fn(),
    });
    const quote = container.querySelector('.reply-quote');
    expect(quote).not.toBeNull();
    expect(quote?.classList.contains('deleted')).toBe(true);
    // Not a button: there is nothing left to jump to.
    expect(quote?.tagName.toLowerCase()).not.toBe('button');
    expect(quote?.textContent).toContain('Original message deleted');
  });

  it('a staged citation with no `deleted` flag renders as live', () => {
    // Absence counts as live — the composer stages without asserting a state
    // only the server knows, so a missing flag must not render as deleted.
    const { container } = render(Message, {
      ...base,
      message: userMsg({ replyTo: { msgId: 22, role: 'assistant', excerpt: 'earlier' } }),
      onJumpToMessage: vi.fn(),
    });
    expect(container.querySelector('.reply-quote')?.classList.contains('deleted')).toBe(false);
  });

  it('renders nothing for a turn that cites nothing', () => {
    const { container } = render(Message, { ...base, message: userMsg() });
    expect(container.querySelector('.reply-quote')).toBeNull();
  });
});
