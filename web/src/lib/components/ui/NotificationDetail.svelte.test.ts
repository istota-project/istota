/**
 * The detail modal, and the rendering rule the whole feature rests on.
 *
 * A gated email's sender and subject are **attacker-supplied**, and they reach
 * this component through `title` and `body`. The server flattens the markup
 * characters that would turn a subject into a live link in a Talk room, but the
 * browser is the surface that would execute one — so the rule here is separate
 * and absolute: every field is a text node, and no component in this feature
 * uses `{@html}`.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, cleanup, screen } from '@testing-library/svelte';
import type { NotificationAction, ResolvedNotification } from '$lib/api';
import NotificationDetail from './NotificationDetail.svelte';

afterEach(cleanup);

const here = dirname(fileURLToPath(import.meta.url));

const action = (over: Partial<NotificationAction> = {}): NotificationAction => ({
  id: 'confirm',
  label: 'Confirm',
  kind: 'primary',
  method: 'POST',
  endpoint: '/chat/tasks/1/confirm',
  href: null,
  ...over,
});

function item(over: Partial<ResolvedNotification> = {}): ResolvedNotification {
  return {
    id: 1,
    source: 'confirmation',
    severity: 'warning',
    actionable: true,
    title: 'Email from a stranger is waiting for your approval',
    body: 'Nothing has been run, and the message body is not shown.',
    link: null,
    occurrences: 1,
    created_at: '2026-08-01T00:00:00.000Z',
    updated_at: '2026-08-01T00:00:00.000Z',
    seen_at: null,
    object_type: 'task',
    object_id: '1',
    actions: [action(), action({ id: 'discard', label: 'Discard', kind: 'danger' })],
    status_note: null,
    ...over,
  };
}

const noop = () => {};

function open(over: Partial<ResolvedNotification> = {}) {
  return render(NotificationDetail, {
    item: item(over),
    open: true,
    onOpenChange: noop,
    onAction: noop,
    onDismiss: noop,
  });
}

describe('untrusted content', () => {
  const HOSTILE = '<img src=x onerror="alert(1)"> [click me](http://evil.example) *bold*';

  it('renders a title containing markup characters as a text node', () => {
    open({ title: HOSTILE });
    // `getByText` matches the text content, so this passing at all means the
    // characters survived as text. The assertions below are what make it
    // falsifiable: with `{@html}` the <img> would be an element and the
    // remaining text would no longer read as one string.
    expect(screen.getByText(HOSTILE)).toBeInTheDocument();
  });

  it('injects no element from a hostile title', () => {
    const { container } = open({ title: HOSTILE });
    expect(container.querySelector('img')).toBeNull();
    expect(container.ownerDocument.querySelector('img')).toBeNull();
  });

  it('injects no element or link from a hostile body', () => {
    const { container } = open({ body: HOSTILE, title: 'Held mail' });
    const doc = container.ownerDocument;
    expect(doc.querySelector('img')).toBeNull();
    // A markdown link would become an anchor; a text node keeps the brackets.
    expect(screen.getByText(HOSTILE)).toBeInTheDocument();
  });

  it('uses no {@html} in any of the five components', () => {
    // The rule is per-file rather than per-render, because the hazard is a
    // future edit rather than today's markup: one such tag added anywhere in
    // this feature makes a stranger's subject line executable, and no rendering
    // test written against today's fields would catch it.
    //
    // Comments are stripped first. Each of these files *documents* the rule in
    // its own header, so a raw-file match reports the prose that states the
    // invariant as a breach of it — DraftCard's own source assertion carries a
    // note about the same trap, having solved it by policing its wording
    // instead. A real tag is in markup, not in a comment, so stripping cannot
    // hide one.
    const strip = (src: string) =>
      src
        .replace(/<!--[\s\S]*?-->/g, '')
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '');
    for (const file of [
      'NotificationDetail.svelte',
      'NotificationItem.svelte',
      'NotificationPanel.svelte',
      'NotificationBell.svelte',
      'CountPill.svelte',
    ]) {
      expect(strip(readFileSync(resolve(here, file), 'utf8'))).not.toMatch(/\{@html/);
    }
  });
});

describe('actions', () => {
  it('renders every action, not just the two the row shows', () => {
    open({
      actions: [
        action(),
        action({ id: 'discard', label: 'Discard', kind: 'danger' }),
        action({ id: 'third', label: 'Third', kind: 'default' }),
      ],
    });
    expect(screen.getByText('Confirm')).toBeInTheDocument();
    expect(screen.getByText('Discard')).toBeInTheDocument();
    expect(screen.getByText('Third')).toBeInTheDocument();
  });

  it('always offers Dismiss', () => {
    // Including on a row nobody can explain: one whose source is gone is still
    // one the user should be able to clear.
    open({ actions: [], status_note: "This notification's source is no longer available." });
    expect(screen.getByText('Dismiss')).toBeInTheDocument();
  });

  it('does not offer an action whose path fails the allowlist', () => {
    // The modal renders every action, so nothing is being truncated here — an
    // action that cannot be issued is simply not offered, rather than rendered
    // as a button that refuses when pressed.
    const { container } = open({
      actions: [
        action({
          id: 'bad',
          label: 'Bad link',
          method: 'LINK',
          endpoint: null,
          href: 'https://evil.example',
        }),
        action({ id: 'trav', label: 'Traversal', endpoint: '/chat/tasks/1/../../admin' }),
        action(),
      ],
    });
    expect(screen.getByText('Confirm')).toBeInTheDocument();
    expect(screen.queryByText('Bad link')).toBeNull();
    expect(screen.queryByText('Traversal')).toBeNull();
    expect(container.ownerDocument.querySelector('a[href*="evil.example"]')).toBeNull();
  });

  it('does not render a link the allowlist refuses', () => {
    const { container } = open({ link: 'javascript:alert(1)' });
    expect(container.ownerDocument.querySelector('.detail-link')).toBeNull();
  });

  it('renders a link the allowlist accepts', () => {
    const { container } = open({ link: '/health/bloodwork' });
    expect(container.ownerDocument.querySelector('a[href$="/health/bloodwork"]')).not.toBeNull();
  });

  it('renders a LINK action as a real anchor', () => {
    const { container } = open({
      actions: [
        action({ id: 'open', label: 'Open', method: 'LINK', endpoint: null, href: '/health' }),
      ],
    });
    const anchor = container.ownerDocument.querySelector('a[href$="/health"]');
    expect(anchor).not.toBeNull();
  });
});

describe('status note', () => {
  it('is shown when there are no actions', () => {
    // "No actions because this draft is mid-send" and "no actions because
    // nobody registered this source" are different things to tell someone; an
    // empty action list alone conflates them.
    open({ actions: [], status_note: 'This message is being sent.' });
    expect(screen.getByText('This message is being sent.')).toBeInTheDocument();
  });

  it('is absent when the source had nothing to say', () => {
    const { container } = open({ status_note: null });
    expect(container.ownerDocument.querySelector('.detail-note')).toBeNull();
  });
});

describe('mounting', () => {
  it('renders nothing without an item', () => {
    const { container } = render(NotificationDetail, {
      item: null,
      open: true,
      onOpenChange: noop,
      onAction: noop,
      onDismiss: noop,
    });
    expect(container.ownerDocument.querySelector('.ui-modal-content')).toBeNull();
  });
});
