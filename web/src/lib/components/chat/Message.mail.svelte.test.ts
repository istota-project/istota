/**
 * A mail the bot sent into an email thread room (ISSUE-612).
 *
 * The answer renders as an ordinary bot turn, and a card under it says who the
 * mail went to and what became of it. The mailed text is shown only when the
 * server sends it, which it does only when it differs from the answer.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

afterEach(cleanup);

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

function botMsg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'assistant',
    text: 'Thursday after 7 works',
    segments: [{ kind: 'text', text: 'Thursday after 7 works' }],
    streaming: false,
    msgId: 9,
    ...over,
  } as ChatMessage;
}

const sent = {
  to: ['carol@example.com'],
  cc: ['alice@example.com', 'bob@example.com'],
  subject: 'Re: Dinner plans',
  state: 'sent' as const,
};

describe('the outgoing-mail card', () => {
  it('names the state, subject and recipients under the answer', () => {
    const { container } = render(Message, { ...base, message: botMsg({ mail: sent }) });
    const card = container.querySelector('[data-testid="outgoing-mail"]');
    expect(card).not.toBeNull();
    expect(card?.textContent).toContain('Sent by email');
    expect(card?.textContent).toContain('Re: Dinner plans');
    expect(card?.textContent).toContain('To: carol@example.com');
    expect(card?.textContent).toContain('Cc: alice@example.com, bob@example.com');
    // The body is the answer above it, not repeated inside the card.
    expect(card?.querySelector('.user-text')).toBeNull();
  });

  it('shows the mailed text when it differs from the answer', () => {
    const { container } = render(Message, {
      ...base,
      message: botMsg({ text: 'I told them.', mail: { ...sent, body: 'Thursday works.' } }),
    });
    const card = container.querySelector('[data-testid="outgoing-mail"]');
    expect(card?.querySelector('.user-text')?.textContent).toBe('Thursday works.');
  });

  it.each([
    ['held', 'Email held for approval'],
    ['failed', 'Email not sent'],
    ['discarded', 'Email discarded'],
  ] as const)('labels a %s mail', (state, label) => {
    const { container } = render(Message, {
      ...base,
      message: botMsg({ mail: { ...sent, state } }),
    });
    expect(container.querySelector('[data-testid="outgoing-mail"]')?.textContent).toContain(label);
  });

  it('is absent on an ordinary answer', () => {
    const { container } = render(Message, { ...base, message: botMsg() });
    expect(container.querySelector('[data-testid="outgoing-mail"]')).toBeNull();
  });
});
