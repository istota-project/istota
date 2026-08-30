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

type QueueHandlers = {
  onQueueSend?: (cid: number) => void;
  onQueueEdit?: (cid: number) => void;
  onQueueRemove?: (cid: number) => void;
};

function mountQueued(over: Partial<ChatMessage>, handlers: QueueHandlers = {}) {
  return render(Message, {
    message: userRow({ sendState: 'queued', ...over }),
    onConfirm: noop,
    onReject: noop,
    ...handlers,
  });
}

/** The visible labels of the queued row's controls, in DOM order. */
function queueButtons(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.send-queued button.btn')].map((b) =>
    (b.textContent ?? '').trim(),
  );
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

/**
 * A message typed into a busy room and waiting its turn (ISSUE-238).
 *
 * Not the assistant placeholder's `Queued…`, which means the opposite — that
 * one has been POSTed and is waiting for its stream; this one has not been
 * POSTed at all, which is why it is still editable and removable.
 */
describe('user row queued state', () => {
  const handlers: QueueHandlers = {
    onQueueSend: noop,
    onQueueEdit: noop,
    onQueueRemove: noop,
  };

  it('says it is waiting, and keeps the message legible', () => {
    const { container } = mountQueued({}, handlers);
    const queued = container.querySelector('.send-queued');
    expect(queued).not.toBeNull();
    expect(queued?.textContent).toContain('Waiting to send');
    // The text the user committed to sending is what they would reread before
    // editing it, so the body renders in full.
    expect(container.textContent).toContain('hello there');
  });

  it('renders the chips and the optimistic quote as an ordinary row would', () => {
    // The whole body, not only the text: the row exists so the user can see
    // what they committed to sending, and a message whose file or citation
    // vanished from the bubble is not the message they wrote. The muting
    // (`.msg.queued`) is what says it has not gone out.
    const { container } = mountQueued(
      {
        attachments: ['notes.pdf'],
        replyTo: { msgId: 7, role: 'assistant', excerpt: 'the earlier answer' },
      },
      handlers,
    );
    expect(container.querySelector('.msg.queued')).not.toBeNull();
    expect(container.querySelector('.attachments')?.textContent).toContain('notes.pdf');
    expect(container.querySelector('.reply-quote')?.textContent).toContain('the earlier answer');
  });

  it('offers Edit and Remove but not Send while the queue is live', () => {
    // An unheld entry drains by itself when the running turn settles. A Send
    // button would race that drain for the one slot `runTurn` owns.
    const { container } = mountQueued({}, handlers);
    expect(queueButtons(container)).toEqual(['Edit', 'Remove']);
  });

  it('says it is waiting for a connection when that is what it is waiting for', () => {
    // The one string the offline outbox adds (ISSUE-202). "Waiting to send" is
    // true but reads as a queue that is about to move; an entry queued with no
    // connection is waiting on something the user can see the banner for.
    const { container } = mountQueued({ queueReason: 'offline' }, handlers);
    expect(container.querySelector('.send-queued')?.textContent).toContain(
      'Waiting for a connection',
    );
  });

  it('says a held offline entry is held, not that it is waiting for anything', () => {
    // Past its auto-send age a restored offline entry is held like any other,
    // and what it waits for then is the user rather than the network.
    const { container } = mountQueued({ queueReason: 'offline', queueHeld: true }, handlers);
    const text = container.querySelector('.send-queued')?.textContent ?? '';
    expect(text).toContain('Held — not sent');
    expect(text).not.toContain('connection');
  });

  it('says it is held and offers Send once the turn ended abnormally', () => {
    const { container } = mountQueued({ queueHeld: true }, handlers);
    expect(container.querySelector('.send-queued')?.textContent).toContain('Held — not sent');
    expect(queueButtons(container)).toEqual(['Send', 'Edit', 'Remove']);
  });

  it('calls each handler with the row it belongs to', async () => {
    const sent: number[] = [];
    const edited: number[] = [];
    const removed: number[] = [];
    const { container } = mountQueued(
      { cid: 12, queueHeld: true },
      {
        onQueueSend: (cid) => sent.push(cid),
        onQueueEdit: (cid) => edited.push(cid),
        onQueueRemove: (cid) => removed.push(cid),
      },
    );
    // The shared `Button` primitive, not a bare styled <button> — see web/AGENTS.md.
    const btns = [...container.querySelectorAll<HTMLButtonElement>('.send-queued button.btn')];
    expect(btns).toHaveLength(3);
    for (const b of btns) await fireEvent.click(b);
    expect(sent).toEqual([12]);
    expect(edited).toEqual([12]);
    expect(removed).toEqual([12]);
  });

  it('offers nothing where the surface passes no handlers', () => {
    // A read-only surface still says what the row is; it just cannot act on it.
    const { container } = mountQueued({ queueHeld: true });
    expect(container.querySelector('.send-queued')?.textContent).toContain('Held — not sent');
    expect(queueButtons(container)).toEqual([]);
  });

  it('withholds the turn actions from a message that has not been sent', () => {
    // Star, reply and delete all act on a durable turn, and this is not one yet.
    const { container } = mountQueued({}, handlers);
    expect(container.querySelector('.turn-actions')).toBeNull();
  });

  it('shows neither the pending nor the failed mark', () => {
    const { container } = mountQueued({}, handlers);
    expect(container.querySelector('.send-pending')).toBeNull();
    expect(container.querySelector('.send-failed')).toBeNull();
  });
});
