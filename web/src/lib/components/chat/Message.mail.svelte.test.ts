/**
 * Mail rows (ISSUE-612; hidden email threads, stage 3).
 *
 * In a mail room every row is a mail. A bot row that went out as a mail
 * renders as the outgoing card alone, its body inside it and no message text
 * above, since the row body is the mailed body. A user row carrying
 * `receivedMail` renders as the incoming card in place of the bubble.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { ChatMessage } from '$lib/stores/segments';
import type { ReceivedMail } from '$lib/api';
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

const card = (c: HTMLElement) => c.querySelector<HTMLElement>('[data-testid="mail-card"]');

describe('an outgoing mail row', () => {
  it('renders the card alone, with the body inside it', () => {
    const { container } = render(Message, { ...base, message: botMsg({ mail: sent }) });
    const mail = card(container);
    expect(mail?.dataset.direction).toBe('out');
    expect(mail?.textContent).toContain('Sent by email');
    expect(mail?.textContent).toContain('Re: Dinner plans');
    expect(mail?.textContent).toContain('To: carol@example.com');
    expect(mail?.textContent).toContain('Thursday after 7 works');
    // No message text above the card.
    expect(container.querySelector('.body.markdown')).toBeNull();
  });

  it('shows the mailed text when it differs from the row', () => {
    const { container } = render(Message, {
      ...base,
      message: botMsg({ text: 'I told them.', mail: { ...sent, body: 'Thursday works.' } }),
    });
    expect(card(container)?.textContent).toContain('Thursday works.');
    expect(card(container)?.textContent).not.toContain('I told them.');
  });

  it.each([
    ['held', 'Held for your approval'],
    ['failed', 'Not sent'],
    ['discarded', 'Discarded'],
  ] as const)('labels a %s mail', (state, label) => {
    const { container } = render(Message, {
      ...base,
      message: botMsg({ mail: { ...sent, state } }),
    });
    expect(container.querySelector('[data-testid="mail-state"]')?.textContent?.trim()).toBe(label);
  });

  it('links a held mail to the private chat and offers no draft controls', () => {
    // The page passes no drafts for a thread row (`draftsForRow`, held by
    // routes/chat/readOnlyThread.svelte.test.ts); this is the card's half.
    const { container, queryByRole, getByText } = render(Message, {
      ...base,
      message: botMsg({ taskId: 9, mail: { ...sent, state: 'held', notePath: '/chat/r/w/t/9' } }),
    });
    expect(getByText('Open in your private chat').closest('a')?.getAttribute('href')).toBe(
      '/chat/r/w/t/9',
    );
    expect(container.querySelector('.draft-card')).toBeNull();
    expect(queryByRole('button', { name: /send|approve/i })).toBeNull();
  });

  it('is absent on an ordinary answer', () => {
    const { container } = render(Message, { ...base, message: botMsg() });
    expect(card(container)).toBeNull();
    expect(container.querySelector('.body.markdown')?.textContent).toContain(
      'Thursday after 7 works',
    );
  });
});

const received: ReceivedMail = {
  from: { name: 'Alice Ash', address: 'alice@example.com' },
  to: [{ name: '', address: 'bot@example.com' }],
  cc: [],
  date: '',
  subject: 'Dinner plans',
  attachments: [],
  new_text: 'Thursday works.',
  rest: '',
  labels: {},
  sender_check: 'none',
  trusted: true,
};

describe('an incoming mail row', () => {
  it('renders the incoming card in place of the bubble', () => {
    const { container } = render(Message, {
      ...base,
      message: {
        cid: 2,
        role: 'user',
        text: 'Thursday works.',
        segments: [],
        streaming: false,
        author: 'alice@example.com',
        origin: 'email',
        subject: 'Dinner plans',
        receivedMail: received,
      } as ChatMessage,
    });
    const mail = card(container);
    expect(mail?.dataset.direction).toBe('in');
    expect(mail?.textContent).toContain('Thursday works.');
    expect(mail?.querySelector('[data-testid="sender-check"]')?.getAttribute('title')).toBe(
      'Trusted sender',
    );
    // Not also the external block.
    expect(container.querySelector('.external')).toBeNull();
  });

  it('keeps the row chips on a pre-change mail, whose card has none', () => {
    const { container } = render(Message, {
      ...base,
      message: {
        cid: 3,
        role: 'user',
        text: 'See attached.',
        segments: [],
        streaming: false,
        attachments: ['report.pdf'],
        receivedMail: { ...received, fallback: true },
      } as ChatMessage,
    });
    expect(container.querySelector('.attachments')?.textContent).toContain('report.pdf');
  });
});

/**
 * ISSUE-660: an email note's incoming and outgoing cards are siblings in
 * `.content`, and `.mail-card` has no outer margin of its own, so the second
 * card sat flush under the first. jsdom applies no component styles and
 * computes no layout, so this checks the two halves apart: the rendered cards
 * are adjacent siblings under `.content`, and `Message.svelte` carries a
 * sibling rule that spaces exactly that shape.
 */
describe('two mail cards in one row', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const src = readFileSync(resolve(here, 'Message.svelte'), 'utf8');
  const style = src
    .slice(src.indexOf('>', src.indexOf('<style')) + 1, src.lastIndexOf('</style>'))
    .replace(/\/\*[\s\S]*?\*\//g, '');

  it("renders an email note's cards as adjacent siblings in .content", () => {
    const { container } = render(Message, {
      ...base,
      message: {
        cid: 4,
        role: 'system',
        text: 'alice@example.com wrote on Dinner plans',
        segments: [],
        streaming: false,
        msgId: 12,
        taskId: 9,
        emailNote: { header: 'alice@example.com wrote on Dinner plans', outcome: '', remark: '' },
        receivedMail: received,
        mail: sent,
      } as ChatMessage,
    });
    const cards = container.querySelectorAll<HTMLElement>('[data-testid="mail-card"]');
    expect(cards.length).toBe(2);
    expect(cards[0].parentElement?.classList.contains('content')).toBe(true);
    expect(cards[0].nextElementSibling).toBe(cards[1]);
  });

  it('spaces a mail card that follows another mail card', () => {
    const body = style.match(
      /\.content\s*>\s*:global\(\.mail-card\)\s*\+\s*:global\(\.mail-card\)\s*\{([^}]*)\}/,
    )?.[1];
    expect(body, 'no sibling rule for consecutive mail cards').toBeDefined();
    expect(body).toMatch(/margin-top:\s*var\(--space-[1-9]\)/);
  });
});
