/**
 * External-origin turn rendering.
 *
 * A user row carrying `origin` came from a surface the room does not live on —
 * today, mail mirrored into the thread it continues. Before this it rendered as
 * an ordinary user bubble with an unfamiliar name in it: full body, no
 * provenance, nothing saying a stranger wrote it.
 *
 * The setting (`externalDisplay`) governs the **body only**. Every case here
 * that asserts the header still renders is asserting the same rule from a
 * different angle: a transcript holding a bot answer with no question above it
 * is the defect the inbound mirror exists to fix, so `hidden` withholds text,
 * never the turn.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

afterEach(cleanup);

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

const BODY = 'Does the west branch work? I need 30 minutes\n\nsecond paragraph';

function externalMsg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'user',
    text: BODY,
    segments: [],
    streaming: false,
    msgId: 41,
    origin: 'email',
    subject: 'Re: Scheduling',
    author: 'contact@example.com',
    ...over,
  };
}

function head(container: HTMLElement): HTMLElement | null {
  return container.querySelector<HTMLElement>('.external-head');
}

function toggle(container: HTMLElement): HTMLButtonElement | null {
  return container.querySelector<HTMLButtonElement>('.external-toggle');
}

describe('the external marker', () => {
  it('marks a turn that came from outside the room', () => {
    const { container } = render(Message, { ...base, message: externalMsg() });
    expect(container.querySelector('.external')).not.toBeNull();
    expect(head(container)?.textContent).toContain('External email');
    expect(head(container)?.textContent).toContain('Re: Scheduling');
  });

  it('leaves an ordinary user turn alone', () => {
    // Absence of `origin` is the signal, so a web or Talk turn — including a
    // co-member's, which also carries an `author` — must be untouched.
    const { container } = render(Message, {
      ...base,
      message: externalMsg({ origin: undefined, subject: undefined, author: 'Bob' }),
    });
    expect(container.querySelector('.external')).toBeNull();
    expect(container.textContent).toContain('Does the west branch work?');
  });

  it('names the sender in the author header', () => {
    // The address is sanitized at write time and rendered as text — the header
    // is where "who" lives, so the external block carries provenance only.
    const { container } = render(Message, { ...base, message: externalMsg() });
    expect(container.querySelector('.author')?.textContent).toBe('contact@example.com');
  });

  it('renders a subject containing markup characters as text', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg({ subject: '<img src=x onerror=alert(1)>' }),
    });
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('.external-subject')?.textContent).toBe(
      '<img src=x onerror=alert(1)>',
    );
  });

  it('renders without a subject when the mail had none', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg({ subject: undefined }),
    });
    expect(head(container)).not.toBeNull();
    expect(container.querySelector('.external-subject')).toBeNull();
  });

  it('does not claim "email" for an origin it does not recognize', () => {
    // The server's contract is "a surface that does not own rooms"
    // (surfaces.is_room_member, read negated), which is email today only
    // because TRANSCRIPT_SURFACE_FILTER limits user rows to web/talk/email.
    // That coupling spans two files with nothing enforcing it,
    // so a widened filter must not have the client assert an email that never
    // arrived — while still marking the turn as from outside.
    const { container } = render(Message, {
      ...base,
      message: externalMsg({ origin: 'ntfy' }),
    });
    expect(container.querySelector('.external')).not.toBeNull();
    expect(head(container)?.textContent).toContain('External message');
    expect(head(container)?.textContent).not.toContain('External email');
  });
});

describe('externalDisplay = full', () => {
  it('shows the whole body, still marked as external', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg(),
      externalDisplay: 'full',
    });
    expect(container.querySelector('.user-text')?.textContent).toBe(BODY);
    expect(head(container)).not.toBeNull();
  });

  it('offers no toggle — there is nothing left to expand', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg(),
      externalDisplay: 'full',
    });
    expect(toggle(container)).toBeNull();
  });
});

describe('externalDisplay = collapsed', () => {
  it('is the default when the prop is not passed', () => {
    const { container } = render(Message, { ...base, message: externalMsg() });
    expect(container.querySelector('.external-preview')).not.toBeNull();
    expect(container.querySelector('.user-text')).toBeNull();
  });

  it('shows the first non-blank line in place of the body', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg({ text: '\n\n  first line here\nsecond line' }),
      externalDisplay: 'collapsed',
    });
    expect(container.querySelector('.external-preview')?.textContent).toBe('first line here');
  });

  it('caps a long first line rather than letting it stand in for the body', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg({ text: 'x'.repeat(400) }),
      externalDisplay: 'collapsed',
    });
    const preview = container.querySelector('.external-preview')?.textContent ?? '';
    expect(preview.length).toBeLessThan(200);
    expect(preview.endsWith('…')).toBe(true);
  });

  it('expands in place and collapses again', async () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg(),
      externalDisplay: 'collapsed',
    });
    const btn = toggle(container);
    expect(btn?.getAttribute('aria-expanded')).toBe('false');

    await fireEvent.click(btn!);
    expect(container.querySelector('.user-text')?.textContent).toBe(BODY);
    expect(container.querySelector('.external-preview')).toBeNull();
    expect(toggle(container)?.getAttribute('aria-expanded')).toBe('true');

    await fireEvent.click(toggle(container)!);
    expect(container.querySelector('.user-text')).toBeNull();
    expect(container.querySelector('.external-preview')).not.toBeNull();
  });

  it('offers no toggle for a turn with no text to expand', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg({ text: '   ' }),
      externalDisplay: 'collapsed',
    });
    expect(toggle(container)).toBeNull();
    expect(head(container)).not.toBeNull();
  });
});

describe('externalDisplay = hidden', () => {
  it('still renders the header row', () => {
    // The load-bearing case: the turn is what tells the reader the bot's answer
    // below is answering something.
    const { container } = render(Message, {
      ...base,
      message: externalMsg(),
      externalDisplay: 'hidden',
    });
    expect(head(container)?.textContent).toContain('External email');
    expect(head(container)?.textContent).toContain('Re: Scheduling');
  });

  it('shows neither the body nor a preview of it', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg(),
      externalDisplay: 'hidden',
    });
    expect(container.querySelector('.user-text')).toBeNull();
    expect(container.querySelector('.external-preview')).toBeNull();
    expect(container.textContent).not.toContain('west branch');
  });

  it('offers no expansion — the setting is a refusal, not a default', () => {
    const { container } = render(Message, {
      ...base,
      message: externalMsg(),
      externalDisplay: 'hidden',
    });
    expect(toggle(container)).toBeNull();
  });

  it('wins over a body the reader expanded under collapsed', async () => {
    // Expand under `collapsed`, then arrive at `hidden`. Trusting the expansion
    // flag on its own left the body on screen with the toggle gone — stuck open
    // in the one mode whose whole job is to withhold it. Not reachable through
    // today's UI, since the prop is only set at init, and one config refresh
    // away from being so.
    const { container, rerender } = render(Message, {
      ...base,
      message: externalMsg(),
      externalDisplay: 'collapsed',
    });
    await fireEvent.click(toggle(container)!);
    expect(container.querySelector('.user-text')).not.toBeNull();

    await rerender({ ...base, message: externalMsg(), externalDisplay: 'hidden' });

    expect(container.querySelector('.user-text')).toBeNull();
    expect(container.querySelector('.external-preview')).toBeNull();
    expect(head(container)).not.toBeNull();
  });
});

describe('the collapsed preview', () => {
  it('does not split a surrogate pair at the cap', () => {
    // `slice` counts UTF-16 code units, so a cut landing between an emoji's
    // surrogates renders a lone one (U+FFFD) right before the ellipsis.
    const { container } = render(Message, {
      ...base,
      message: externalMsg({ text: `${'a'.repeat(159)}😀 tail` }),
      externalDisplay: 'collapsed',
    });
    const preview = container.querySelector('.external-preview')?.textContent ?? '';
    expect(preview).not.toContain('�');
    expect(preview.endsWith('…')).toBe(true);
    // The emoji is the 160th code point, so it survives whole.
    expect(preview).toContain('😀');
  });
});
