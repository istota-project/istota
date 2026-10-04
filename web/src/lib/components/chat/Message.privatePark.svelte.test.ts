/**
 * A question parked privately about another room (#624).
 *
 * The preview arrives as a `role='system'` row in the member's private room,
 * tagged with the room it is about. The row speaks in the bot's voice, and the
 * confirmation card sits under it while its task waits.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

afterEach(cleanup);

const noop = () => {};

function privateRow(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 7,
    role: 'system',
    text: 'Post this in Family as Istota?',
    segments: [],
    streaming: false,
    msgId: 42,
    aboutRoom: { token: 'rm_family', name: 'Family' },
    ...over,
  };
}

describe('a private reply in the bot voice', () => {
  it('takes the bot avatar and name instead of the notice mark', () => {
    const { container } = render(Message, {
      message: privateRow(),
      onConfirm: noop,
      onReject: noop,
      botName: 'Istota',
    });
    expect(container.querySelector('.gutter .sys-mark')).toBeNull();
    expect(container.querySelector('.gutter .fallback, .gutter img')).not.toBeNull();
    expect(container.querySelector('.cmd-row .author')?.textContent).toBe('Istota');
    expect(container.querySelector('.about-chip')?.textContent).toContain('re: Family');
  });

  it('leaves an ordinary notice with its mark', () => {
    const { container } = render(Message, {
      message: privateRow({ aboutRoom: undefined }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.gutter .sys-mark')).not.toBeNull();
    expect(container.querySelector('.cmd-row .author')).toBeNull();
  });
});

describe('the card under a parked row', () => {
  it('renders while the row is a confirmation and answers for its task', async () => {
    const onConfirm = vi.fn();
    const onReject = vi.fn();
    const { container, getByText } = render(Message, {
      message: privateRow({ confirmation: true, taskId: 31 }),
      onConfirm,
      onReject,
    });
    expect(container.querySelector('.cmd-row .content .confirm-card')).not.toBeNull();
    await fireEvent.click(getByText('Confirm'));
    expect(onConfirm).toHaveBeenCalledWith(7, 31);
    await fireEvent.click(getByText('Cancel'));
    expect(onReject).toHaveBeenCalledWith(7, 31);
  });

  it('is absent once the row is no longer a confirmation', () => {
    const { container } = render(Message, {
      message: privateRow({ taskId: 31 }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.confirm-card')).toBeNull();
  });
});
