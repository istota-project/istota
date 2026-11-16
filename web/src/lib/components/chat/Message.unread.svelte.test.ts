import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

// Read on view: an unread row in an aggregate pane carries a dot, which clears
// once the room's read cursor passes it. The room view never draws it.

afterEach(cleanup);

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

function row(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'assistant',
    text: 'the answer',
    streaming: false,
    segments: [{ kind: 'text', id: 's1', text: 'the answer', settled: true }],
    createdAt: '2026-10-08T12:00:00Z',
    msgId: 42,
    taskId: 365,
    roomToken: 't1',
    roomName: 'Room 1',
    unread: true,
    ...over,
  };
}

const mark = (container: HTMLElement) => container.querySelector('[aria-label="Unread"]');

describe('the unread mark', () => {
  it('draws in the header line of an unread row in an aggregate pane', () => {
    const { container } = render(Message, {
      ...base,
      message: row(),
      aggregate: true,
      unreadMarks: true,
      onRoomClick: noop,
    });
    expect(mark(container)?.closest('.meta')).not.toBeNull();
  });

  it('draws in the gutter of a continuation row', () => {
    const { container } = render(Message, {
      ...base,
      message: row(),
      continuation: true,
      aggregate: true,
      unreadMarks: true,
    });
    expect(mark(container)?.closest('.gutter')).not.toBeNull();
  });

  it('draws on a system row', () => {
    const { container } = render(Message, {
      ...base,
      message: row({ role: 'system', text: 'a notice', segments: [] }),
      aggregate: true,
      unreadMarks: true,
    });
    expect(mark(container)).not.toBeNull();
  });

  it('is gone once the row is read', () => {
    const { container } = render(Message, {
      ...base,
      message: row({ unread: false }),
      aggregate: true,
      unreadMarks: true,
    });
    expect(mark(container)).toBeNull();
  });

  it('is not drawn in the room view', () => {
    const { container } = render(Message, {
      ...base,
      message: row(),
      unreadMarks: true,
    });
    expect(mark(container)).toBeNull();
  });

  it('is not drawn where the pane does not read on view', () => {
    const { container } = render(Message, { ...base, message: row(), aggregate: true });
    expect(mark(container)).toBeNull();
  });
});
