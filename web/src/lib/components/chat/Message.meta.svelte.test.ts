import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

// ISSUE-691: the hover metadata floated over a continuation row's first line,
// and a turn carried two stars. The metadata now sits in the turn action row,
// the action row's star is the only star, and a starred row is marked at rest
// by an amber rule along its left edge.

afterEach(cleanup);

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

function turn(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'assistant',
    text: 'the answer',
    streaming: false,
    segments: [{ kind: 'text', id: 's1', text: 'the answer', settled: true }],
    createdAt: '2026-10-08T12:00:00Z',
    msgId: 42,
    taskId: 365,
    model: 'anthropic/claude-opus-5-5',
    durationSeconds: 8,
    ...over,
  };
}

const metaText = (container: HTMLElement) =>
  container.querySelector('.turn-actions .meta-footer')?.textContent?.trim() ?? '';

describe('metadata lives in the turn action row', () => {
  it('is in the action row on a continuation row with a reply quote, not over it', () => {
    const { container } = render(Message, {
      ...base,
      message: turn({ replyTo: { msgId: 3, role: 'user', excerpt: 'the quoted line' } }),
      continuation: true,
      onToggleStar: noop,
    });

    expect(metaText(container)).toBe('#365 · opus-5-5 · 8s');
    expect(container.querySelector('.msg-actions')).toBeNull();
    // Nothing positioned in the content column's first line competes with the quote.
    expect(container.querySelector('.reply-quote')?.textContent).toContain('the quoted line');
  });

  it('leaves the fresh-group header to author, time and chips', () => {
    const { container } = render(Message, { ...base, message: turn(), onToggleStar: noop });

    const header = container.querySelector('.meta')!;
    expect(header.querySelector('.meta-footer')).toBeNull();
    expect(header.querySelector('button[aria-label$="tar message"]')).toBeNull();
    expect(metaText(container)).toContain('#365');
  });

  it('is right of the buttons in the same row', () => {
    const { container } = render(Message, {
      ...base,
      message: turn(),
      onToggleStar: noop,
      onDelete: noop,
    });

    const row = container.querySelector('.turn-actions')!;
    expect(row.lastElementChild?.classList.contains('meta-footer')).toBe(true);
  });

  it('shows the action row for metadata alone', () => {
    // No handlers and nothing to copy: the row still carries the task line.
    const { container } = render(Message, {
      ...base,
      message: turn({ text: '', segments: [] }),
    });

    expect(container.querySelectorAll('.turn-action')).toHaveLength(0);
    expect(metaText(container)).toContain('#365');
  });

  it('is withheld on a streaming turn', () => {
    const { container } = render(Message, {
      ...base,
      message: turn({ streaming: true }),
      onToggleStar: noop,
    });

    expect(container.querySelector('.meta-footer')).toBeNull();
  });

  it('reveals on touch only with the active row, without changing the markup', async () => {
    // A row holding only metadata (a stopped turn with no stored row) must not
    // grow when a tap opens it, so the span is there either way and the row's
    // `.revealed` opacity is what shows it.
    const message = turn({ msgId: undefined, text: '', segments: [] });
    const { container, rerender } = render(Message, { ...base, message, touch: true });

    const row = container.querySelector('.turn-actions')!;
    expect(row.classList.contains('revealed')).toBe(false);
    expect(row.innerHTML).toContain('#365');
    const before = row.innerHTML;

    await rerender({ ...base, message, touch: true, active: true });

    expect(row.classList.contains('revealed')).toBe(true);
    expect(row.innerHTML).toBe(before);
  });

  it('shows only the task number in the aggregate views', () => {
    const { container } = render(Message, {
      ...base,
      message: turn(),
      aggregate: true,
      onToggleStar: noop,
    });

    expect(metaText(container)).toBe('#365');
  });
});

describe('one star per turn', () => {
  it('renders exactly one star toggle on a turn', () => {
    const { container } = render(Message, {
      ...base,
      message: turn({ starred: true }),
      onToggleStar: noop,
    });

    expect(container.querySelectorAll('button[aria-pressed]')).toHaveLength(1);
    expect(container.querySelector('.turn-actions .turn-action.star')).not.toBeNull();
  });

  it('renders exactly one star toggle on a system row', () => {
    const { container } = render(Message, {
      ...base,
      message: turn({ role: 'system', text: 'Backup finished.', starred: true }),
      onToggleStar: noop,
    });

    expect(container.querySelectorAll('button[aria-pressed]')).toHaveLength(1);
    expect(container.querySelector('.cmd-actions')).toBeNull();
  });

  it('renders no star icon outside the hidden action row on a starred touch row', () => {
    const { container } = render(Message, {
      ...base,
      message: turn({ starred: true }),
      onToggleStar: noop,
      touch: true,
    });

    const stars = container.querySelectorAll('button[aria-pressed]');
    expect(stars).toHaveLength(1);
    expect(stars[0].closest('.turn-actions')).not.toBeNull();
  });
});

describe('a starred row is marked at rest', () => {
  it('carries the starred class and a hidden label on a turn', () => {
    const { container } = render(Message, {
      ...base,
      message: turn({ starred: true }),
      onToggleStar: noop,
    });

    const row = container.querySelector('.msg')!;
    expect(row.classList.contains('starred')).toBe(true);
    expect(row.querySelector('.starred-label')?.textContent?.trim()).toBe('Starred');
  });

  it('marks a continuation row and a system row the same way', () => {
    const cont = render(Message, {
      ...base,
      message: turn({ starred: true }),
      continuation: true,
      onToggleStar: noop,
    });
    expect(cont.container.querySelector('.msg.starred .starred-label')).not.toBeNull();
    cont.unmount();

    const { container } = render(Message, {
      ...base,
      message: turn({ role: 'system', text: 'Alert', starred: true }),
      onToggleStar: noop,
    });
    expect(container.querySelector('.cmd-row.starred .starred-label')).not.toBeNull();
  });

  it('renders no mark on an unstarred row', () => {
    const { container } = render(Message, {
      ...base,
      message: turn({ starred: false }),
      onToggleStar: noop,
    });

    expect(container.querySelector('.msg')?.classList.contains('starred')).toBe(false);
    expect(container.querySelector('.starred-label')).toBeNull();
  });

  it('follows the toggle', async () => {
    const { container, rerender } = render(Message, {
      ...base,
      message: turn({ starred: false }),
      onToggleStar: noop,
    });
    await rerender({ ...base, message: turn({ starred: true }), onToggleStar: noop });

    expect(container.querySelector('.msg')?.classList.contains('starred')).toBe(true);
  });

  it('draws the mark as a bordered pseudo-element inside the inline padding', () => {
    // jsdom applies no stylesheet, so the CSS half is read from source. A
    // border survives forced-colors mode where box-shadow is dropped, and a
    // real border on the row would shift the avatar and text off the shared
    // column. The vertical inset is what keeps two starred rows two segments.
    const source = readFileSync(resolve(__dirname, 'Message.svelte'), 'utf8');
    const rule = source.match(/\.msg\.starred::before,\s*\.cmd-row\.starred::before\s*\{([^}]*)\}/);
    expect(rule).not.toBeNull();
    const body = rule![1];
    expect(body).toMatch(/position:\s*absolute/);
    expect(body).toMatch(/left:\s*0/);
    expect(body).toMatch(/border-left:\s*4px solid var\(--accent-amber\)/);
    expect(body).toMatch(/top:\s*var\(--space-1\)/);
    expect(body).toMatch(/bottom:\s*var\(--space-1\)/);
    expect(body).not.toMatch(/box-shadow/);
  });
});
