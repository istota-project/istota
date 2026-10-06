/**
 * The mail card (hidden email threads, stage 3): one component for a mail
 * that came in and one that went out, in the thread room, the private email
 * room and, collapsed, under a private note.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import type { ReceivedMail } from '$lib/api';
import MailCard from './MailCard.svelte';
import { receivedCard, sentCard } from './mailCard';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const received = (over: Partial<ReceivedMail> = {}): ReceivedMail => ({
  from: { name: 'Alice Ash', address: 'alice@ext.example' },
  to: [{ name: '', address: 'bot@test.com' }],
  cc: [
    { name: '', address: 'carol@test.com' },
    { name: 'Bob', address: 'bob@ext.example' },
    { name: '', address: 'dave@ext.example' },
    { name: '', address: 'erin@ext.example' },
  ],
  date: 'not a date',
  subject: 'Re: Dinner plans',
  attachments: [],
  new_text: 'Thursday works.',
  rest: 'On Mon, Carol wrote:\n> Dinner?',
  labels: { 'carol@test.com': 'you', 'bot@test.com': 'Zorg' },
  message_id: '<a2@ext.example>',
  in_reply_to: '<root@test.com>',
  sender_check: 'none',
  trusted: false,
  ...over,
});

const card = () => document.querySelector<HTMLElement>('[data-testid="mail-card"]')!;
const text = () => card().textContent?.replace(/\s+/g, ' ') ?? '';

async function openMenu() {
  await fireEvent.keyDown(screen.getByLabelText('Mail actions'), { key: 'Enter' });
}

describe('an incoming card', () => {
  it('names the direction, sender, subject and recipients', () => {
    render(MailCard, { card: receivedCard(received()) });
    expect(card().dataset.direction).toBe('in');
    expect(text()).toContain('Received by email');
    expect(text()).toContain('Alice Ash');
    expect(text()).toContain('alice@ext.example');
    expect(text()).toContain('Re: Dinner plans');
    // The bot is named, not addressed.
    expect(text()).toContain('To: Zorg');
  });

  it('renders the viewer as "you" and folds a fourth Cc into "+1 more"', async () => {
    render(MailCard, { card: receivedCard(received()) });
    expect(text()).toContain('Cc: you, Bob, dave@ext.example +1 more');
    expect(text()).not.toContain('erin@ext.example');
    await fireEvent.click(screen.getByRole('button', { name: '+1 more' }));
    expect(text()).toContain('erin@ext.example');
  });

  it('shows the new text and keeps the rest behind "Show quoted text"', async () => {
    render(MailCard, { card: receivedCard(received()) });
    expect(text()).toContain('Thursday works.');
    expect(text()).not.toContain('Carol wrote');
    await fireEvent.click(screen.getByRole('button', { name: 'Show quoted text' }));
    expect(text()).toContain('On Mon, Carol wrote:');
  });

  it('renders the body and the quoted text as markdown', async () => {
    render(MailCard, {
      card: receivedCard(
        received({
          new_text: 'Sent from [Proton Mail](https://proton.me/mail/home) for iOS.',
          rest: 'On Mon, Carol wrote:\n> Dinner?',
        }),
      ),
    });
    const link = screen.getByRole('link', { name: 'Proton Mail' });
    expect(link.getAttribute('href')).toBe('https://proton.me/mail/home');
    expect(text()).toContain('Sent from Proton Mail (proton.me) for iOS.');
    expect(text()).not.toContain('](');
    await fireEvent.click(screen.getByRole('button', { name: 'Show quoted text' }));
    const quote = card().querySelector('.mail-quoted blockquote');
    expect(quote?.textContent?.trim()).toBe('Dinner?');
  });

  it('names where a relabelled link in a received body goes', () => {
    render(MailCard, {
      card: receivedCard(
        received({ new_text: 'Log in at [https://bank.example](https://evil.example/x)' }),
      ),
    });
    expect(text()).toContain('https://bank.example (evil.example)');
  });

  it('does not draw a workspace image from a received body', () => {
    render(MailCard, {
      card: receivedCard(received({ new_text: '![badge](/api/chat/files?path=/Users/u/x.png)' })),
    });
    expect(card().querySelector('img')).toBeNull();
  });

  it('does not emit raw HTML from a mail body', () => {
    render(MailCard, {
      card: receivedCard(received({ new_text: '<img src=x onerror="alert(1)"> hi' })),
    });
    expect(card().querySelector('img')).toBeNull();
    expect(text()).toContain('<img src=x');
  });

  const marks = () =>
    [...card().querySelectorAll<HTMLElement>('[data-testid="sender-check"]')].map((m) => ({
      title: m.getAttribute('title'),
      label: m.getAttribute('aria-label'),
      warn: m.classList.contains('mail-check-warn'),
      onFromLine: !!m.closest('[data-testid="mail-from"]'),
    }));

  it.each([
    [{ trusted: true }, 'Trusted sender', false],
    [{ sender_check: 'verified' as const }, 'Verified sender', false],
    [{ sender_check: 'none' as const }, 'Unverified sender', false],
    [{ sender_check: 'failed' as const }, 'Failed sender check', true],
  ])('marks the sender check with an icon on the From line %#', (over, title, warn) => {
    render(MailCard, { card: receivedCard(received(over)) });
    expect(marks()).toEqual([{ title, label: title, warn, onFromLine: true }]);
    // The text is the hover and the accessible name, not a visible badge.
    expect(text()).not.toContain(title);
  });

  it('shows the trust mark and a visible warning for a trusted sender that failed', () => {
    render(MailCard, { card: receivedCard(received({ trusted: true, sender_check: 'failed' })) });
    expect(marks()).toEqual([
      { title: 'Trusted sender', label: 'Trusted sender', warn: false, onFromLine: true },
      { title: 'Failed sender check', label: 'Failed sender check', warn: true, onFromLine: true },
    ]);
  });

  it('renders no badges row when there is no state to show', () => {
    render(MailCard, { card: receivedCard(received()) });
    expect(card().querySelector('.mail-badges')).toBeNull();
  });

  it('puts the address beside a display name that reads as a label', () => {
    render(MailCard, {
      card: receivedCard(
        received({ cc: [{ name: 'you', address: 'eve@evil.example' }], labels: {} }),
      ),
    });
    expect(text()).toContain('Cc: you <eve@evil.example>');
  });

  it('links an attachment in the workspace and leaves the rest as text', () => {
    render(MailCard, {
      card: receivedCard(
        received({
          attachments: [
            { filename: 'report.pdf', size: 2048, path: '/Users/carol/inbox/ab_report.pdf' },
            { filename: 'huge.zip', size: 9_000_000 },
          ],
        }),
      ),
    });
    const linked = screen.getByText(/report\.pdf/).closest('a');
    expect(linked?.getAttribute('href')).toContain('ab_report.pdf');
    expect(screen.getByText(/2 KB/)).toBeTruthy();
    expect(screen.getByText(/huge\.zip/).closest('a')).toBeNull();
  });

  it('copies the sender address from the menu', async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    render(MailCard, { card: receivedCard(received()) });
    await openMenu();
    await fireEvent.click(await screen.findByText('Copy address'));
    expect(writeText).toHaveBeenCalledWith('alice@ext.example');
  });

  it('goes to the note from "Discuss in private chat"', async () => {
    render(MailCard, { card: receivedCard(received({ note_path: '/chat/r/web-1/t/7' })) });
    await openMenu();
    const link = (await screen.findByText('Discuss in private chat')).closest('a');
    expect(link?.getAttribute('href')).toBe('/chat/r/web-1/t/7');
    expect(link?.hasAttribute('data-sveltekit-reload')).toBe(true);
  });

  it('with no note, "Discuss in private chat" hands over the private room and the thread', async () => {
    const onDiscuss = vi.fn();
    render(MailCard, {
      card: receivedCard(
        received({ note_path: '/chat/r/web-1/t/7', discuss: { room: 'web-1', about: 'thr-1' } }),
      ),
      onDiscuss,
    });
    await openMenu();
    const item = await screen.findByText('Discuss in private chat');
    expect(item.closest('a')).toBeNull();
    await fireEvent.click(item);
    expect(onDiscuss).toHaveBeenCalledWith({ room: 'web-1', about: 'thr-1' });
  });

  it('with no handler, falls back to the link', async () => {
    render(MailCard, {
      card: receivedCard(
        received({ note_path: '/chat/r/web-1/t/7', discuss: { room: 'web-1', about: 'thr-1' } }),
      ),
    });
    await openMenu();
    const link = (await screen.findByText('Discuss in private chat')).closest('a');
    expect(link?.getAttribute('href')).toBe('/chat/r/web-1/t/7');
  });

  it('lists the stored headers from "Show headers"', async () => {
    render(MailCard, { card: receivedCard(received()) });
    await openMenu();
    await fireEvent.click(await screen.findByText('Show headers'));
    const headers = card().querySelector('[data-testid="mail-headers"]')!.textContent ?? '';
    expect(headers).toContain('<a2@ext.example>');
    expect(headers).toContain('<root@test.com>');
    expect(headers).toContain('not a date');
    expect(headers).toContain('Unverified sender');
  });

  it('renders a pre-change row from the wrapper with "Show headers" hidden', async () => {
    render(MailCard, {
      card: receivedCard({
        fallback: true,
        from: { name: '', address: 'alice@ext.example' },
        to: [],
        cc: [],
        attachments: [],
        date: 'Mon, 01 Jan 2026',
        subject: 'Dinner plans',
        new_text: 'hello',
        rest: '',
        labels: {},
      }),
    });
    expect(text()).toContain('alice@ext.example');
    expect(text()).toContain('Dinner plans');
    expect(card().querySelector('[data-testid="sender-check"]')).toBeNull();
    await openMenu();
    expect(await screen.findByText('Copy address')).toBeTruthy();
    expect(screen.queryByText('Show headers')).toBeNull();
  });
});

describe('an outgoing card', () => {
  const sent = {
    to: ['alice@ext.example'],
    cc: ['carol@test.com'],
    subject: 'Re: Dinner plans',
    state: 'sent' as const,
    labels: { 'carol@test.com': 'you' },
  };

  it('carries the mailed body and the state, and no sender check', () => {
    render(MailCard, { card: sentCard(sent, 'Thursday after 7 works') });
    expect(card().dataset.direction).toBe('out');
    expect(text()).toContain('Sent by email');
    expect(text()).toContain('Thursday after 7 works');
    expect(text()).toContain('Cc: you');
    // Mailed as plain text, so it reads as typed rather than rendered.
    expect(card().querySelector('.mail-body.markdown')).toBeNull();
    expect(card().querySelector('[data-testid="mail-state"]')?.textContent?.trim()).toBe('Sent');
    expect(card().querySelector('[data-testid="sender-check"]')).toBeNull();
  });

  it('shows the sent body as typed', () => {
    render(MailCard, { card: sentCard(sent, 'See [the menu](https://example.com/menu)') });
    expect(text()).toContain('See [the menu](https://example.com/menu)');
    expect(card().querySelector('a[href="https://example.com/menu"]')).toBeNull();
  });

  it('copies the first recipient', async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    render(MailCard, { card: sentCard(sent, 'x') });
    await openMenu();
    await fireEvent.click(await screen.findByText('Copy address'));
    expect(writeText).toHaveBeenCalledWith('alice@ext.example');
  });

  it('shows a held mail with a link to the private chat and no controls', () => {
    render(MailCard, {
      card: sentCard({ ...sent, state: 'held', notePath: '/chat/r/web-1/t/9' }, 'x'),
    });
    expect(card().querySelector('[data-testid="mail-state"]')?.textContent?.trim()).toBe(
      'Held for your approval',
    );
    const link = screen.getByText('Open in your private chat').closest('a');
    expect(link?.getAttribute('href')).toBe('/chat/r/web-1/t/9');
    for (const control of ['Send', 'Approve', 'Edit', 'Discard']) {
      expect(screen.queryByRole('button', { name: control })).toBeNull();
    }
  });

  it('collapses to one line that expands', async () => {
    render(MailCard, { card: sentCard(sent, 'Thursday after 7 works'), collapsed: true });
    const line = card().querySelector<HTMLButtonElement>('[data-testid="mail-collapsed"]')!;
    expect(line.textContent?.replace(/\s+/g, ' ')).toContain('Sent by email');
    expect(line.textContent).toContain('To alice@ext.example');
    expect(line.textContent).toContain('Re: Dinner plans');
    expect(line.textContent).toContain('Sent');
    expect(text()).not.toContain('Thursday after 7 works');
    await fireEvent.click(line);
    expect(text()).toContain('Thursday after 7 works');
  });
});
