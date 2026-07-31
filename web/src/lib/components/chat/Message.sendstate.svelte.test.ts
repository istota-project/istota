/**
 * How a user row reports its own send (ISSUE-200).
 *
 * The failure has to land on the message that failed. Before this, a send
 * error was written into the *assistant* placeholder, so "your message never
 * left" and "the assistant errored replying" both presented as the latter.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

afterEach(cleanup);

const noop = () => {};

function userRow(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'user',
    text: 'hello there',
    segments: [],
    streaming: false,
    createdAt: '2026-07-10T12:00:00Z',
    ...over,
  };
}

function mount(message: ChatMessage, onRetry?: (cid: number) => void) {
  return render(Message, { message, onConfirm: noop, onReject: noop, onRetry });
}

describe('user row send state', () => {
  it('shows neither mark on a settled row', () => {
    const { container } = mount(userRow());
    expect(container.querySelector('.send-pending')).toBeNull();
    expect(container.querySelector('.send-failed')).toBeNull();
  });

  it('stays quiet while the send is merely in flight', () => {
    // `sendState` is truthful from the moment the row exists; the store's grace
    // timer decides when it is worth showing, via `showSending`.
    const { container } = mount(userRow({ sendState: 'sending' }));
    expect(container.querySelector('.send-pending')).toBeNull();
  });

  it('shows the pending mark once the send is slow', () => {
    const { container } = mount(userRow({ sendState: 'sending', showSending: true }));
    expect(container.querySelector('.send-pending')).not.toBeNull();
  });

  it('renders the failure and its reason on the user row itself', () => {
    const { container } = mount(
      userRow({
        sendState: 'failed',
        sendError: 'Couldn’t send — the server is unreachable.',
        retryable: true,
      }),
    );
    const failed = container.querySelector('.send-failed');
    expect(failed).not.toBeNull();
    expect(failed?.textContent).toContain('unreachable');
    // The message stays legible — it is what the user would reread before retrying.
    expect(container.textContent).toContain('hello there');
  });

  it('offers Retry on a retryable failure and calls back with the row', async () => {
    const seen: number[] = [];
    const { container } = mount(
      userRow({ cid: 9, sendState: 'failed', sendError: 'nope', retryable: true }),
      (cid) => seen.push(cid),
    );
    // The shared `Button` primitive, not a bare styled <button> — see web/AGENTS.md.
    const btn = container.querySelector<HTMLButtonElement>('.send-failed button.btn');
    expect(btn).not.toBeNull();
    await fireEvent.click(btn!);
    expect(seen).toEqual([9]);
  });

  it('offers no Retry when retrying cannot succeed', () => {
    const { container } = mount(
      userRow({
        sendState: 'failed',
        sendError: 'Your session expired. Reload to sign in again.',
        retryable: false,
      }),
      noop,
    );
    expect(container.querySelector('.send-failed')).not.toBeNull();
    expect(container.querySelector('.send-failed button')).toBeNull();
  });

  it('offers no Retry when the surface passes no handler', () => {
    const { container } = mount(userRow({ sendState: 'failed', retryable: true }));
    expect(container.querySelector('.send-failed button')).toBeNull();
  });

  it('withholds the turn actions from a message that never landed', () => {
    // Copy, star and delete all act on a durable turn. A failed send has none.
    const { container } = mount(
      userRow({ sendState: 'failed', sendError: 'nope', retryable: true }),
      noop,
    );
    expect(container.querySelector('.turn-actions')).toBeNull();
  });
});
