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

describe('the mail card under an email note (hidden email threads, stage 5)', () => {
  const note = () =>
    privateRow({
      text: 'Ana wrote on Book club:\n\n> Thursday?\n\nReplied.',
      taskId: 9,
      mail: {
        to: ['ana@example.com'],
        cc: [],
        subject: 'Re: Book club',
        state: 'sent',
        body: 'Thursday works for us.',
      },
    });

  it('renders collapsed to one line and expands to the card', async () => {
    const { container, getByTestId } = render(Message, {
      message: note(),
      onConfirm: noop,
      onReject: noop,
    });
    const line = getByTestId('mail-collapsed');
    expect(container.querySelector('.cmd-row .content [data-testid="mail-card"]')).not.toBeNull();
    expect(line.textContent).toContain('Sent by email');
    expect(line.textContent).toContain('To ana@example.com');
    expect(line.textContent).toContain('Re: Book club');
    expect(container.textContent).not.toContain('Thursday works for us.');
    await fireEvent.click(line);
    expect(container.querySelector('[data-testid="mail-collapsed"]')).toBeNull();
    expect(container.textContent).toContain('Thursday works for us.');
  });

  it('is absent on a note whose turn mailed nothing', () => {
    const { container } = render(Message, {
      message: privateRow({ taskId: 9 }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('[data-testid="mail-card"]')).toBeNull();
  });
});

describe('a bot-voice row reads as the bot talking (ISSUE-644)', () => {
  it('renders a private reply as bot text, with no notice panel', () => {
    const { container } = render(Message, {
      message: privateRow(),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.cmd-output')).toBeNull();
    expect(container.querySelector('.cmd-row .content .body.markdown')?.textContent).toContain(
      'Post this in Family as Istota?',
    );
  });

  it('keeps the panel on an ordinary notice', () => {
    const { container } = render(Message, {
      message: privateRow({ aboutRoom: undefined }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.cmd-output')).not.toBeNull();
    expect(container.querySelector('.cmd-row .body')).toBeNull();
  });
});

describe('an email note with its parts (ISSUE-644)', () => {
  const received = {
    from: { name: '', address: 'ana@example.com' },
    to: [{ name: '', address: 'bot@example.com' }],
    cc: [],
    date: 'Sun, 04 Oct 2026 10:00:00 +0000',
    subject: 'Dinner',
    attachments: [],
    new_text: 'Friday?',
    rest: 'Sent from my phone',
    labels: {},
  };
  const parts = {
    header: 'ana@example.com wrote on Book club, without you on the message',
    outcome: 'Replied.',
    remark: 'Your calendar is free on **Friday**.',
  };
  const noteRow = (over: Partial<ChatMessage> = {}) =>
    privateRow({
      text: 'ana@example.com wrote on Book club:\n\n> Friday?\n\nReplied.\n\nYour calendar…',
      taskId: 9,
      emailNote: parts,
      receivedMail: received,
      ...over,
    });
  const sent = {
    to: ['ana@example.com'],
    cc: [],
    subject: 'Re: Dinner',
    state: 'sent' as const,
    body: 'Friday works.',
  };

  it('shows the header, the incoming mail as a card and the remark as bot text', () => {
    const { container } = render(Message, {
      message: noteRow({ mail: sent }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.cmd-output')).toBeNull();
    expect(container.querySelector('.note-header')?.textContent).toBe(parts.header);
    const cards = container.querySelectorAll('[data-testid="mail-card"]');
    expect(cards.length).toBe(2);
    // The quote is not rendered; the mail card carries the mail.
    expect(container.textContent).not.toContain('> Friday?');
    const remark = container.querySelector('.note-remark');
    expect(remark?.querySelector('strong')?.textContent).toBe('Friday');
    // The sent card already says the outcome.
    expect(container.querySelector('.note-outcome')).toBeNull();
  });

  it('says the outcome where no card does', () => {
    const { container } = render(Message, {
      message: noteRow({ emailNote: { ...parts, outcome: 'No reply sent.', remark: '' } }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.note-outcome')?.textContent).toBe('No reply sent.');
    expect(container.querySelector('.note-remark')).toBeNull();
  });

  it('puts a parked question above its card and drops the outcome line', () => {
    const { container } = render(Message, {
      message: noteRow({
        emailNote: { ...parts, outcome: 'Question for you.', remark: 'Shall I say Friday?' },
        confirmation: true,
      }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.note-outcome')).toBeNull();
    const remark = container.querySelector('.note-remark');
    const card = container.querySelector('.confirm-card');
    expect(remark && card).toBeTruthy();
    expect(remark!.compareDocumentPosition(card!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders the body when the parts arrive without their mail', () => {
    const { container } = render(Message, {
      message: noteRow({ receivedMail: undefined }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.note-header')).toBeNull();
    expect(container.textContent).toContain('Friday?');
  });

  it('renders a note from before the parts from its body, as bot text', () => {
    const { container } = render(Message, {
      message: noteRow({ emailNote: undefined, receivedMail: undefined }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.note-header')).toBeNull();
    expect(container.querySelector('.cmd-output')).toBeNull();
    expect(container.querySelector('.body.markdown blockquote')?.textContent).toContain('Friday?');
  });
});
