/**
 * `sticky` — a notice that states a condition rather than an event.
 *
 * The rest of `notices.ts` assumes an event: something happened, the user reads
 * it once, and it goes. Three mechanisms enforce that assumption, and each one
 * would take a condition off screen while it was still true — the navigation
 * clear, the `PINNED_HANDOVER_MS` handover, and the `MAX_QUEUE` trim. A sticky
 * notice is exempt from all three.
 *
 * The exemption it deliberately does **not** get is holding the slot. Silencing
 * the channel for as long as the condition lasts is the very hazard the
 * handover exists to prevent, so a sticky head steps aside for events and takes
 * the slot back when they are done.
 *
 * Every case here is written so it fails against a plain `duration: 0` notice —
 * which is what the first attempt at chat's offline notice used, and what the
 * three mechanisms above quietly defeated.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';
import {
  notify,
  notifyError,
  notifyWarning,
  dismissNotice,
  clearNotices,
  notices,
  currentNotice,
  MAX_QUEUE,
  PINNED_HANDOVER_MS,
} from './notices';

/** The condition under test: what chat raises while it cannot reach the server. */
const condition = () =>
  notifyWarning('Offline — messages will send when you’re back.', {
    duration: 0,
    sticky: true,
    key: 'chat:offline',
  });

const keys = () => get(notices).map((n) => n.key);
const headKey = () => get(currentNotice)?.key ?? null;

beforeEach(() => {
  vi.useFakeTimers();
  clearNotices();
  // clearNotices now spares sticky notices, so a test that left one behind
  // would seed the next. Take them down explicitly.
  for (const n of get(notices)) dismissNotice(n.id);
});

afterEach(() => {
  for (const n of get(notices)) dismissNotice(n.id);
  vi.useRealTimers();
});

describe('a sticky notice against the three ways an event is retracted', () => {
  it('survives the navigation clear', () => {
    condition();
    notify('an ordinary event');
    expect(keys()).toHaveLength(2);

    clearNotices();

    // The event was a comment on the surface being navigated away from. The
    // condition is not: the connection is still down after a route change.
    expect(keys()).toEqual(['chat:offline']);
  });

  it('is never dismissed by the handover, however long something waits behind it', () => {
    condition();
    notifyError('Couldn’t send that message.');

    vi.advanceTimersByTime(PINNED_HANDOVER_MS * 3);

    expect(keys()).toContain('chat:offline');
  });

  it('is not the victim when the queue is trimmed', () => {
    condition();
    // The first event pushes the condition off the head, so it is a *queued*
    // entry from here on — which is the only place the trim ever looks. Without
    // that step the condition sits at index 0, the trim cannot reach it, and
    // this passes whether or not it is protected.
    notify('first event', { key: 'e-first' });
    expect(headKey()).not.toBe('chat:offline');

    for (let i = 0; i < MAX_QUEUE + 3; i += 1) notify(`event ${i}`, { key: `e${i}` });

    expect(keys()).toContain('chat:offline');
    expect(get(notices).length).toBeLessThanOrEqual(MAX_QUEUE);
  });
});

describe('a sticky notice yielding the one slot', () => {
  it('steps aside as soon as an event arrives, rather than making it wait', () => {
    condition();
    expect(headKey()).toBe('chat:offline');

    notifyError('Couldn’t send that message.');

    // Not after PINNED_HANDOVER_MS — immediately. A failed send queued behind a
    // condition that outlasts it would otherwise be unreadable for 30s, which
    // is the same silencing the handover exists to prevent, by a slower route.
    expect(headKey()).toBe('error:Couldn’t send that message.');
  });

  it('takes the slot back when the event is dismissed', () => {
    condition();
    const errId = notifyError('Couldn’t send that message.');
    expect(headKey()).not.toBe('chat:offline');

    dismissNotice(errId);

    expect(headKey()).toBe('chat:offline');
  });

  it('takes the slot back when the event expires on its own clock', () => {
    condition();
    notify('saved', { severity: 'success' });
    expect(headKey()).not.toBe('chat:offline');

    vi.advanceTimersByTime(10_000);

    expect(headKey()).toBe('chat:offline');
  });

  it('does not spin when every notice is sticky', () => {
    condition();
    notify('a second condition', { duration: 0, sticky: true, key: 'other:condition' });

    // Nothing non-sticky to yield to, so the head stays put rather than
    // rotating forever. The guard that makes the rotation terminate.
    expect(headKey()).toBe('chat:offline');
    expect(keys()).toHaveLength(2);
  });
});

describe('what sticky does not change', () => {
  it('leaves an ordinary pinned notice on the handover it always had', () => {
    const errId = notifyError('something failed');
    notify('something else happened');

    vi.advanceTimersByTime(PINNED_HANDOVER_MS + 100);

    // Not sticky, so the existing rule still applies: it hands the slot over so
    // one unanswered error cannot silence the channel for the life of the tab.
    expect(get(notices).some((n) => n.id === errId)).toBe(false);
  });

  it('defaults to false, so nothing already in the app becomes sticky', () => {
    notify('an ordinary event');
    expect(get(notices)[0].sticky).toBe(false);

    clearNotices();
    expect(keys()).toHaveLength(0);
  });
});
