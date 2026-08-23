/**
 * The approval card for a held outbound email.
 *
 * The single promise the feature makes is that approving sends exactly the
 * bytes the user read, so most of what is asserted here is about the card not
 * abbreviating, not re-rendering and not offering an action that cannot work.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import type { OutboundDraft } from '$lib/api';
import DraftCard from './DraftCard.svelte';

afterEach(cleanup);

function draft(over: Partial<OutboundDraft> = {}): OutboundDraft {
  return {
    id: 41,
    status: 'pending',
    room_token: 'rm1',
    task_id: 7,
    subject: 'Re: Invite',
    body: 'Wednesday at two works.',
    html: false,
    to: ['stranger@example.invalid'],
    cc: [],
    bcc: [],
    attachments: [],
    hold_reason: 'untrusted_recipient',
    created_at: '2026-08-10T12:00:00Z',
    actions_taken: [],
    ...over,
  };
}

function mount(d: OutboundDraft, handlers: Record<string, unknown> = {}) {
  return render(DraftCard, {
    draft: d,
    onApprove: () => true,
    onDiscard: () => true,
    onEdit: () => true,
    ...handlers,
  });
}

function buttonNamed(container: HTMLElement, label: string) {
  return [...container.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);
}

describe('a pending draft', () => {
  it('renders the recipients, subject and the whole body', () => {
    const { container } = mount(draft());
    expect(container.textContent).toContain('stranger@example.invalid');
    expect(container.textContent).toContain('Re: Invite');
    expect(container.textContent).toContain('Wednesday at two works.');
  });

  it('lists cc and bcc alongside the To recipients', () => {
    // One untrusted address holds the whole message, so the card has to show
    // everyone it would reach — not just the To line.
    const { container } = mount(draft({ cc: ['b@x.invalid'], bcc: ['c@x.invalid'] }));
    expect(container.textContent).toContain('b@x.invalid');
    expect(container.textContent).toContain('c@x.invalid');
  });

  it('renders the body as text, never as markup', () => {
    // The body is composed from a thread with a stranger. Rendering it as HTML
    // is the injection this whole feature exists to make harder.
    const { container } = mount(draft({ body: '<img src=x onerror=alert(1)>' }));
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('says why the message is held', () => {
    const { container } = mount(draft({ hold_reason: 'all_mode' }));
    expect(container.textContent).toContain('every outbound message needs approval');
  });

  it('offers send, edit and discard', () => {
    const { container } = mount(draft());
    expect(buttonNamed(container, 'Send')).toBeDefined();
    expect(buttonNamed(container, 'Edit')).toBeDefined();
    expect(buttonNamed(container, 'Discard')).toBeDefined();
  });

  it('calls back with the draft id on send', async () => {
    const onApprove = vi.fn(() => true);
    const { container } = mount(draft({ id: 99 }), { onApprove });
    await fireEvent.click(buttonNamed(container, 'Send')!);
    expect(onApprove).toHaveBeenCalledWith(99);
  });

  it('calls back with the draft id on discard', async () => {
    const onDiscard = vi.fn(() => true);
    const { container } = mount(draft({ id: 99 }), { onDiscard });
    await fireEvent.click(buttonNamed(container, 'Discard')!);
    expect(onDiscard).toHaveBeenCalledWith(99);
  });

  it('does not fire a second time while one action is in flight', async () => {
    // Sending is irreversible, and a double tap is the ordinary way to ask for
    // it twice. The server refuses the second one, but the card must not send
    // it in the first place.
    let release: (v: boolean) => void = () => {};
    const onApprove = vi.fn(() => new Promise<boolean>((r) => (release = r)));
    const { container } = mount(draft(), { onApprove });
    const send = buttonNamed(container, 'Send')!;
    await fireEvent.click(send);
    await fireEvent.click(send);
    expect(onApprove).toHaveBeenCalledTimes(1);
    release(true);
  });

  it('lists what else the task did', () => {
    // Calendar writes are not gated, so declining can leave an orphan event.
    const { container } = mount(
      draft({ actions_taken: ['Created calendar event: Coffee, Wed 14:00'] }),
    );
    expect(container.textContent).toContain('Created calendar event: Coffee, Wed 14:00');
  });

  it('says nothing about other actions when the task took none', () => {
    const { container } = mount(draft({ actions_taken: [] }));
    expect(container.textContent).not.toContain('This task also');
  });
});

describe('a long body', () => {
  const long = 'x'.repeat(900);

  it('collapses to a preview with an expander', () => {
    const { container } = mount(draft({ body: long }));
    const shown = container.querySelector('.draft-body')!.textContent ?? '';
    expect(shown.length).toBeLessThan(long.length);
    expect(buttonNamed(container, 'Show the whole message')).toBeDefined();
  });

  it('expands in place', async () => {
    const { container } = mount(draft({ body: long }));
    await fireEvent.click(buttonNamed(container, 'Show the whole message')!);
    expect(container.querySelector('.draft-body')!.textContent).toContain(long);
  });

  it('adds no whitespace of its own around the body', () => {
    // The block is `white-space: pre-wrap`, so any indentation the formatter
    // puts between the tag and the interpolation renders as leading blanks on
    // the message the user is being asked to approve. Splitting the text across
    // two expressions is what lets that happen, so the assertion is on the
    // rendered node rather than on the source.
    const { container } = mount(draft({ body: 'Wednesday at two works.' }));
    expect(container.querySelector('.draft-body')!.textContent).toBe('Wednesday at two works.');
  });

  it('sends the whole body, not the preview', async () => {
    // The truncation is a rendering decision. What gets sent is the stored row,
    // and what an edit posts is seeded from the full text.
    const onEdit = vi.fn(() => true);
    const { container } = mount(draft({ body: long }), { onEdit });
    await fireEvent.click(buttonNamed(container, 'Edit')!);
    await fireEvent.click(buttonNamed(container, 'Save')!);
    expect(onEdit).toHaveBeenCalledWith(41, long);
  });
});

describe('the banner placement is a compact shape, not a shorter one', () => {
  // Shortening the preview alone did not work: a body with a blank line still
  // renders as two paragraphs under `pre-wrap`, and the fields grid, the
  // actions list and the button row are each a row of their own — so two held
  // drafts still took most of the pane. Compact replaces all of that with four
  // rows and expands to the full card in place.
  const twoParas = 'First paragraph here.\n\nSecond paragraph here.';

  it('defaults to the turn placement, which is the full card', () => {
    const { container } = mount(draft({ body: twoParas }));
    expect(container.querySelector('.draft-body')).not.toBeNull();
    expect(container.querySelector('.draft-peek')).toBeNull();
  });

  it('replaces the body block with a single peek line', () => {
    const { container } = mount(draft({ body: twoParas }), { placement: 'banner' });
    expect(container.querySelector('.draft-body')).toBeNull();
    expect(container.querySelector('.draft-peek')).not.toBeNull();
  });

  it('collapses the whitespace that made a short preview three rows tall', () => {
    // The defect this shape exists for: `pre-wrap` honours the blank line, so
    // even a clipped body occupied a paragraph per break.
    const { container } = mount(draft({ body: twoParas }), { placement: 'banner' });
    const peek = container.querySelector('.draft-peek')!.textContent ?? '';
    expect(peek).not.toMatch(/\n/);
    expect(peek).toBe('First paragraph here. Second paragraph here.');
  });

  it('folds the recipients and subject onto one line', () => {
    // The fields grid spends a row each on To and Subject.
    const { container } = mount(draft(), { placement: 'banner' });
    expect(container.querySelector('.draft-fields')).toBeNull();
    expect(container.querySelector('.draft-summary')!.textContent).toBe(
      'stranger@example.invalid · Re: Invite',
    );
  });

  it('withholds the actions list until expanded', () => {
    const { container } = mount(draft({ actions_taken: ['Created calendar event: Coffee'] }), {
      placement: 'banner',
    });
    expect(container.textContent).not.toContain('This task also');
  });

  it('still offers every action while compact', () => {
    // Compact hides detail, not decisions — the point is answering without
    // opening anything.
    const { container } = mount(draft(), { placement: 'banner' });
    expect(buttonNamed(container, 'Send')).toBeDefined();
    expect(buttonNamed(container, 'Edit')).toBeDefined();
    expect(buttonNamed(container, 'Discard')).toBeDefined();
  });

  it('expands to the full card in place, and back', async () => {
    const { container } = mount(draft({ body: twoParas, actions_taken: ['Created an event'] }), {
      placement: 'banner',
    });
    await fireEvent.click(buttonNamed(container, 'Show the whole message')!);

    expect(container.querySelector('.draft-fields')).not.toBeNull();
    expect(container.querySelector('.draft-body')!.textContent).toBe(twoParas);
    expect(container.textContent).toContain('This task also');

    await fireEvent.click(buttonNamed(container, 'Show less')!);
    expect(container.querySelector('.draft-peek')).not.toBeNull();
  });

  it('sends the whole body from the compact card', async () => {
    // The peek is a label for the held message, never the message. What Edit
    // seeds and what Send releases is the full stored body.
    const onEdit = vi.fn(() => true);
    const { container } = mount(draft({ body: twoParas }), { placement: 'banner', onEdit });
    await fireEvent.click(buttonNamed(container, 'Edit')!);
    await fireEvent.click(buttonNamed(container, 'Save')!);
    expect(onEdit).toHaveBeenCalledWith(41, twoParas);
  });

  it('renders the peek as text, never as markup', () => {
    // Same rule as the body: it is composed from a stranger's thread.
    const { container } = mount(draft({ body: '<img src=x onerror=alert(1)>' }), {
      placement: 'banner',
    });
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('.draft-peek')!.textContent).toContain('<img src=x');
  });

  it('leaves a stuck or unreadable row alone', () => {
    // Those branches are checked before `compact` and carry no body to peek at.
    const stuck = mount(draft({ status: 'sending' }), { placement: 'banner' }).container;
    expect(stuck.textContent).toContain('Check your Sent folder');
    expect(stuck.querySelector('.draft-peek')).toBeNull();

    const bad = mount(draft({ unreadable: true }), { placement: 'banner' }).container;
    expect(bad.querySelector('.draft-peek')).toBeNull();
    expect(buttonNamed(bad, 'Send')).toBeUndefined();
  });
});

describe('the readable-width cap belongs to the slot, not the card', () => {
  /**
   * Asserted against the source rather than a computed style, in the idiom
   * `Composer.sendButton.svelte.test.ts` sets out: jsdom does not apply a Svelte
   * component's own `<style>` at all, so `getComputedStyle(card).maxWidth` reads
   * `none` whether or not the rule is there — vacuous in exactly the direction
   * that matters.
   *
   * The invariant: `--chat-body-max` is what makes a draft card stop where the
   * prose body above it stops, which is right inline under a turn and wrong in
   * `banner` placement, where the card is chrome spanning its container.
   * Carried on the component it applied in both, and in the chat pane's own
   * strip the banner draft stopped 134px short of the confirmation card
   * directly above it. That strip is gone; the placement is still a prop.
   */
  const here = dirname(fileURLToPath(import.meta.url));
  const styleOf = (file: string) => {
    const src = readFileSync(resolve(here, file), 'utf8');
    const open = src.indexOf('>', src.indexOf('<style'));
    return src.slice(open + 1, src.lastIndexOf('</style>')).replace(/\/\*[\s\S]*?\*\//g, '');
  };

  it('is absent from DraftCard', () => {
    expect(styleOf('DraftCard.svelte')).not.toMatch(/max-width/);
  });

  it('is present on the turn slot in Message.svelte', () => {
    const css = styleOf('Message.svelte');
    const rule = css.match(/\.content\s*>\s*:global\(\.draft-card\)\s*\{([^}]*)\}/);
    expect(rule).not.toBeNull();
    expect(rule![1]).toMatch(/max-width:\s*var\(--chat-body-max\)/);
  });
});

describe('editing', () => {
  it('swaps the body for a textarea seeded with it', async () => {
    const { container } = mount(draft());
    await fireEvent.click(buttonNamed(container, 'Edit')!);
    const area = container.querySelector<HTMLTextAreaElement>('textarea.draft-edit');
    expect(area).not.toBeNull();
    expect(area!.value).toBe('Wednesday at two works.');
  });

  it('posts the edited text and returns to the reading view', async () => {
    const onEdit = vi.fn(() => true);
    const { container } = mount(draft(), { onEdit });
    await fireEvent.click(buttonNamed(container, 'Edit')!);
    const area = container.querySelector<HTMLTextAreaElement>('textarea.draft-edit')!;
    await fireEvent.input(area, { target: { value: 'Thursday, actually.' } });
    await fireEvent.click(buttonNamed(container, 'Save')!);
    expect(onEdit).toHaveBeenCalledWith(41, 'Thursday, actually.');
    expect(container.querySelector('textarea.draft-edit')).toBeNull();
  });

  it('keeps the editor open when the save is refused', async () => {
    // Losing the typed text on a refusal would be the second failure, and the
    // worse one — the row is still held, so the edit is still wanted.
    const { container } = mount(draft(), { onEdit: () => false });
    await fireEvent.click(buttonNamed(container, 'Edit')!);
    const area = container.querySelector<HTMLTextAreaElement>('textarea.draft-edit')!;
    await fireEvent.input(area, { target: { value: 'Thursday, actually.' } });
    await fireEvent.click(buttonNamed(container, 'Save')!);
    const still = container.querySelector<HTMLTextAreaElement>('textarea.draft-edit');
    expect(still).not.toBeNull();
    expect(still!.value).toBe('Thursday, actually.');
  });

  it('discards the edit on cancel and leaves the stored body showing', async () => {
    const onEdit = vi.fn(() => true);
    const { container } = mount(draft(), { onEdit });
    await fireEvent.click(buttonNamed(container, 'Edit')!);
    const area = container.querySelector<HTMLTextAreaElement>('textarea.draft-edit')!;
    await fireEvent.input(area, { target: { value: 'nope' } });
    await fireEvent.click(buttonNamed(container, 'Cancel')!);
    expect(onEdit).not.toHaveBeenCalled();
    expect(container.querySelector('.draft-body')!.textContent).toContain(
      'Wednesday at two works.',
    );
  });

  it('offers no way to change the recipients', async () => {
    // An editable recipient list is a gate the user can be talked through,
    // which is the failure this whole feature exists to prevent.
    const { container } = mount(draft());
    await fireEvent.click(buttonNamed(container, 'Edit')!);
    expect(container.querySelectorAll('textarea, input')).toHaveLength(1);
  });
});

describe('a row stuck in sending', () => {
  it('says the mail may already have gone out', () => {
    const { container } = mount(draft({ status: 'sending' }));
    expect(container.textContent).toContain('Sent folder');
  });

  it('offers no action at all', () => {
    // Nobody can know whether the message went out, so every action here is a
    // guess — and one of them would send it twice.
    const { container } = mount(draft({ status: 'sending' }));
    expect(container.querySelectorAll('button')).toHaveLength(0);
  });

  it('still names the message it is about', () => {
    const { container } = mount(draft({ status: 'sending' }));
    expect(container.textContent).toContain('Re: Invite');
  });
});

describe('a row that could not be read', () => {
  const broken: OutboundDraft = { id: 12, unreadable: true };

  it('names it rather than leaving it out', () => {
    const { container } = mount(broken);
    expect(container.textContent).toContain('12');
    expect(container.textContent).toContain('could not be read');
  });

  it('offers discard and nothing else', () => {
    // Discarding sends nothing, so it does not depend on reading the row —
    // and without it the card is stuck on screen with no action that works.
    const { container } = mount(broken);
    expect(container.querySelectorAll('button')).toHaveLength(1);
    expect(buttonNamed(container, 'Discard')).toBeDefined();
  });

  it('does not claim a body it does not have', () => {
    const { container } = mount(broken);
    expect(container.querySelector('.draft-body')).toBeNull();
  });
});

describe('a stub from the stream', () => {
  it('asks for the full row exactly once', () => {
    const onNeedsFullRow = vi.fn();
    mount({ id: 5, status: 'pending', room_token: 'rm1', truncated: true }, { onNeedsFullRow });
    expect(onNeedsFullRow).toHaveBeenCalledTimes(1);
  });

  it('offers no action until the body has arrived', () => {
    // Approving a message the card cannot show is exactly what the gate exists
    // to prevent.
    const { container } = mount({ id: 5, status: 'pending', truncated: true });
    expect(container.querySelectorAll('button')).toHaveLength(0);
  });

  it('asks for nothing when the row is whole', () => {
    const onNeedsFullRow = vi.fn();
    mount(draft(), { onNeedsFullRow });
    expect(onNeedsFullRow).not.toHaveBeenCalled();
  });

  it('asks again when the same card is stubbed a second time', async () => {
    // The stream frame is diffed against the server's own baseline, which the
    // client's refetch does not touch — so the same draft is stubbed again on
    // the next frame that changes anything. A once-per-instance latch left the
    // card on "Loading the held message…" forever, with no way to answer mail
    // that was waiting.
    const onNeedsFullRow = vi.fn();
    const stub: OutboundDraft = { id: 5, status: 'pending', truncated: true };
    const { rerender } = mount(stub, { onNeedsFullRow });
    expect(onNeedsFullRow).toHaveBeenCalledTimes(1);

    await rerender({ draft: draft({ id: 5 }) });
    await rerender({ draft: stub });

    expect(onNeedsFullRow).toHaveBeenCalledTimes(2);
  });

  it('shows the loading state rather than claiming a sending row has no subject', async () => {
    // A stub carries a status but no subject, so checking `sending` first made
    // a stuck row render "(no subject)" — on the card whose job is to help find
    // that message in the Sent folder.
    const { container } = mount({ id: 5, status: 'sending', truncated: true });
    expect(container.textContent).not.toContain('(no subject)');
    expect(container.textContent).toContain('Loading');
  });
});
