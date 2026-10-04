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

  it.each([
    [{ trusted: true, sender_check: 'failed' as const }, 'Trusted sender'],
    [{ sender_check: 'verified' as const }, 'Verified sender'],
    [{ sender_check: 'none' as const }, 'Unverified sender'],
    [{ sender_check: 'failed' as const }, 'Failed sender check'],
  ])('badges the sender %#', (over, badge) => {
    render(MailCard, { card: receivedCard(received(over)) });
    expect(card().querySelector('[data-testid="sender-badge"]')?.textContent?.trim()).toBe(badge);
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
    expect(card().querySelector('[data-testid="sender-badge"]')).toBeNull();
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

  it('carries the mailed body and the state, and no sender badge', () => {
    render(MailCard, { card: sentCard(sent, 'Thursday after 7 works') });
    expect(card().dataset.direction).toBe('out');
    expect(text()).toContain('Sent by email');
    expect(text()).toContain('Thursday after 7 works');
    expect(text()).toContain('Cc: you');
    expect(card().querySelector('[data-testid="mail-state"]')?.textContent?.trim()).toBe('Sent');
    expect(card().querySelector('[data-testid="sender-badge"]')).toBeNull();
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
