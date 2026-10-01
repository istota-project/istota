/**
 * A texted turn and a phone task's question, as a read-only phone room shows
 * them (room-surface-model Stage 24).
 *
 * A turn texted in is the writer's own words, so it carries a provenance line
 * with the surface's icon and none of the external treatment. A parked question
 * in that room was asked by text and is answered by text, so its card has no
 * buttons; the server refuses the answer from web either way.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

afterEach(cleanup);

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

function userMsg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'user',
    text: 'what is on friday',
    segments: [],
    streaming: false,
    msgId: 41,
    ...over,
  };
}

describe('the texted-turn mark', () => {
  it('marks an SMS turn and leaves the body whole', () => {
    const { container } = render(Message, { ...base, message: userMsg({ via: 'sms' }) });
    expect(container.querySelector('.via-mark')?.textContent).toContain('Sent by SMS');
    expect(container.querySelector('.via-mark svg')).not.toBeNull();
    expect(container.querySelector('.external')).toBeNull();
    expect(container.querySelector('.user-text')?.textContent).toBe('what is on friday');
  });

  it('names WhatsApp for a WhatsApp turn', () => {
    const { container } = render(Message, { ...base, message: userMsg({ via: 'whatsapp' }) });
    expect(container.querySelector('.via-mark')?.textContent).toContain('Sent on WhatsApp');
    const mark = container.querySelector<HTMLElement>('.via-mark [data-whatsapp]');
    expect(mark?.dataset.whatsapp).toBe('private');
    expect(mark?.querySelector('svg')?.getAttribute('fill')).toBe('currentColor');
  });

  it('draws the group glyph for a WhatsApp turn in a group room', () => {
    const { container } = render(Message, {
      ...base,
      message: userMsg({ via: 'whatsapp' }),
      phoneGroup: true,
    });
    const mark = container.querySelector<HTMLElement>('.via-mark [data-whatsapp]');
    expect(mark?.dataset.whatsapp).toBe('group');
    expect(mark?.querySelector('svg')?.getAttribute('fill')).toBe('none');
  });

  it('marks nothing on a typed turn', () => {
    const { container } = render(Message, { ...base, message: userMsg() });
    expect(container.querySelector('.via-mark')).toBeNull();
  });
});

describe('the question in a read-only phone room', () => {
  const asked: ChatMessage = {
    cid: 2,
    role: 'assistant',
    text: 'Delete the event?',
    segments: [],
    streaming: false,
    taskId: 7,
    confirmation: true,
  };

  it('says to answer by text and offers no buttons', () => {
    const onConfirm = vi.fn();
    const { container, getByText } = render(Message, {
      ...base,
      onConfirm,
      message: asked,
      answerByText: 'SMS',
    });
    expect(getByText('Reply by SMS to answer.')).toBeTruthy();
    const labels = [...container.querySelectorAll('.confirm-card button')].map((b) =>
      b.textContent?.trim(),
    );
    expect(labels).not.toContain('Confirm');
    expect(labels).not.toContain('Cancel');
  });

  it('keeps the buttons in an ordinary room', () => {
    const { container } = render(Message, { ...base, message: asked });
    const labels = [...container.querySelectorAll('.confirm-card button')].map((b) =>
      b.textContent?.trim(),
    );
    expect(labels).toEqual(['Confirm', 'Cancel']);
  });
});
