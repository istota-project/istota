/**
 * The mail card's data, one shape for both directions (hidden email threads,
 * stage 3). Every field comes from stored metadata or the stored body, never
 * from model text and never from a raw header read at render time.
 */
import type { MailAddress, MailAttachment, OutgoingMailState, ReceivedMail } from '$lib/api';
import type { ChatMessage } from '$lib/stores/segments';

export type MailDirection = 'in' | 'out';

export interface MailCardData {
  direction: MailDirection;
  from?: MailAddress;
  to: MailAddress[];
  cc: MailAddress[];
  subject: string;
  date: string;
  body: string;
  rest: string;
  attachments: MailAttachment[];
  labels: Record<string, string>;
  notePath?: string;
  // No stored metadata behind the card: no recipients, attachments or badge,
  // and no headers to show.
  fallback: boolean;
  senderCheck?: 'verified' | 'failed' | 'none';
  trusted?: boolean;
  messageId?: string;
  inReplyTo?: string;
  state?: OutgoingMailState;
}

export function receivedCard(mail: ReceivedMail): MailCardData {
  return {
    direction: 'in',
    from: mail.from,
    to: mail.to ?? [],
    cc: mail.cc ?? [],
    subject: mail.subject ?? '',
    date: mail.date ?? '',
    body: mail.new_text ?? '',
    rest: mail.rest ?? '',
    attachments: mail.attachments ?? [],
    labels: mail.labels ?? {},
    notePath: mail.note_path || undefined,
    fallback: !!mail.fallback,
    senderCheck: mail.fallback ? undefined : mail.sender_check,
    trusted: mail.fallback ? undefined : mail.trusted,
    messageId: mail.message_id || undefined,
    inReplyTo: mail.in_reply_to || undefined,
  };
}

/** An outgoing card: the mailed text is `mail.body` where it differs from the
 *  row, which is otherwise the mailed body itself. */
export function sentCard(
  mail: NonNullable<ChatMessage['mail']>,
  text: string,
  date = '',
): MailCardData {
  return {
    direction: 'out',
    to: mail.to.map((address) => ({ name: '', address })),
    cc: mail.cc.map((address) => ({ name: '', address })),
    subject: mail.subject ?? '',
    date,
    body: mail.body ?? text,
    rest: '',
    attachments: [],
    labels: mail.labels ?? {},
    notePath: mail.notePath,
    fallback: true,
    state: mail.state,
  };
}

/** How a card names one address: "you", the bot's name, the header's display
 *  name, or the address itself. */
export function addressLabel(person: MailAddress, labels: Record<string, string>): string {
  const assigned = labels[person.address.toLowerCase()];
  if (assigned) return assigned;
  const name = person.name.trim();
  // A display name is the sender's text: one reading as a label the server
  // assigns ("you", the bot's name) gets its address beside it.
  const reserved = new Set(['you', ...Object.values(labels)].map((l) => l.toLowerCase()));
  if (name && reserved.has(name.toLowerCase())) return `${name} <${person.address}>`;
  return name || person.address;
}

export function formatSize(bytes: number | undefined): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** A failed sender check on a trusted sender: the trust list matches the
 *  From address alone, so a forged trusted address is the case to flag. */
export function trustedButFailed(card: MailCardData): boolean {
  return (
    card.direction === 'in' && !card.fallback && !!card.trusted && card.senderCheck === 'failed'
  );
}

/** The incoming card's sender badge; null on an outgoing or fallback card. */
export function senderBadge(card: MailCardData): string | null {
  if (card.direction !== 'in' || card.fallback) return null;
  if (card.trusted) return 'Trusted sender';
  if (card.senderCheck === 'verified') return 'Verified sender';
  if (card.senderCheck === 'failed') return 'Failed sender check';
  return 'Unverified sender';
}

export const MAIL_STATE_LABELS: Record<OutgoingMailState, string> = {
  sent: 'Sent',
  held: 'Held for your approval',
  failed: 'Not sent',
  discarded: 'Discarded',
};

/** A header date as the reader's locale writes it, or as stored when it does
 *  not parse. */
export function formatMailDate(value: string): string {
  if (!value) return '';
  const when = new Date(value);
  return Number.isNaN(when.getTime())
    ? value
    : when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
