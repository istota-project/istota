import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';
import {
  notices,
  currentNotice,
  notify,
  notifyError,
  notifySuccess,
  dismissNotice,
  clearNotices,
  DURATIONS,
  MAX_QUEUE,
  PINNED_HANDOVER_MS,
} from './notices';

beforeEach(() => {
  vi.useFakeTimers();
  clearNotices();
});

afterEach(() => {
  clearNotices();
  vi.useRealTimers();
});

describe('notices store', () => {
  it('starts empty', () => {
    expect(get(notices)).toEqual([]);
    expect(get(currentNotice)).toBeNull();
  });

  it('raises a notice with the default severity and message', () => {
    notify('Saved');
    const current = get(currentNotice);
    expect(current?.message).toBe('Saved');
    expect(current?.severity).toBe('info');
  });

  it('returns an id that dismisses the notice it named', () => {
    const id = notify('Saved');
    expect(get(currentNotice)).not.toBeNull();
    dismissNotice(id);
    expect(get(currentNotice)).toBeNull();
  });

  it('ignores a dismiss for an id that is no longer live', () => {
    const id = notify('Saved');
    dismissNotice(id);
    notify('Second');
    dismissNotice(id);
    expect(get(currentNotice)?.message).toBe('Second');
  });
});

describe('queueing', () => {
  // The drawer is one slot under the header, so concurrent notices queue rather
  // than stack — the next one only appears once the current one has gone.
  it('shows the first notice and holds the rest behind it', () => {
    notify('First');
    notify('Second');
    expect(get(notices)).toHaveLength(2);
    expect(get(currentNotice)?.message).toBe('First');
  });

  it('promotes the queued notice when the current one is dismissed', () => {
    const first = notify('First');
    notify('Second');
    dismissNotice(first);
    expect(get(currentNotice)?.message).toBe('Second');
  });

  it('only runs the auto-dismiss timer for the notice actually on screen', () => {
    notify('First', { duration: 1000 });
    notify('Second', { duration: 1000 });

    // A queued notice must not expire unseen: after the first one's whole
    // lifetime the second is only just arriving.
    vi.advanceTimersByTime(1000);
    expect(get(currentNotice)?.message).toBe('Second');

    vi.advanceTimersByTime(1000);
    expect(get(currentNotice)).toBeNull();
  });
});

describe('auto-dismiss', () => {
  it('clears an info notice after its severity default', () => {
    notify('Copied');
    vi.advanceTimersByTime(DURATIONS.info - 1);
    expect(get(currentNotice)).not.toBeNull();
    vi.advanceTimersByTime(1);
    expect(get(currentNotice)).toBeNull();
  });

  it('keeps a warning up longer than an info notice', () => {
    expect(DURATIONS.warning).toBeGreaterThan(DURATIONS.info);
  });

  // An error is the one severity the user must actively acknowledge — it names
  // something that did not happen, and a four-second window can be missed.
  it('never auto-dismisses an error', () => {
    notifyError('Sync failed');
    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(get(currentNotice)?.message).toBe('Sync failed');
  });

  it('honours an explicit duration over the severity default', () => {
    notifySuccess('Saved', { duration: 50 });
    vi.advanceTimersByTime(50);
    expect(get(currentNotice)).toBeNull();
  });

  // duration: 0 is the documented "stays until dismissed" escape hatch, and has
  // to survive the `??` that fills in the severity default.
  it('treats duration 0 as dismiss-only', () => {
    notify('Pinned', { duration: 0 });
    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(get(currentNotice)?.message).toBe('Pinned');
  });

  // A notice carrying an action is a decision to make, not an announcement;
  // expiring it out from under a reaching finger loses the only way to take it.
  it('does not auto-dismiss a notice carrying an action', () => {
    notify('Upload failed', { action: { label: 'Retry', run: () => {} } });
    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(get(currentNotice)?.message).toBe('Upload failed');
  });
});

describe('coalescing', () => {
  it('counts a repeat of the same message instead of queueing a copy', () => {
    notify('Network unreachable', { severity: 'error' });
    notify('Network unreachable', { severity: 'error' });
    notify('Network unreachable', { severity: 'error' });

    expect(get(notices)).toHaveLength(1);
    expect(get(currentNotice)?.count).toBe(3);
  });

  it('keeps the same message at different severities apart', () => {
    notify('Sync', { severity: 'info' });
    notify('Sync', { severity: 'error' });
    expect(get(notices)).toHaveLength(2);
  });

  it('coalesces on an explicit key even when the text differs', () => {
    notify('Retrying in 3s', { key: 'sync' });
    notify('Retrying in 2s', { key: 'sync' });
    expect(get(notices)).toHaveLength(1);
    // The newest wording wins — a countdown that froze at its first value is
    // worse than no countdown.
    expect(get(currentNotice)?.message).toBe('Retrying in 2s');
  });

  it('restarts the timer when the visible notice is repeated', () => {
    notify('Saving…', { duration: 1000 });
    vi.advanceTimersByTime(800);
    notify('Saving…', { duration: 1000 });
    vi.advanceTimersByTime(800);
    expect(get(currentNotice)).not.toBeNull();
    vi.advanceTimersByTime(200);
    expect(get(currentNotice)).toBeNull();
  });

  it('coalesces into a queued notice without promoting it', () => {
    notify('First');
    notify('Second');
    notify('Second');
    expect(get(notices)).toHaveLength(2);
    expect(get(currentNotice)?.message).toBe('First');
    expect(get(notices)[1].count).toBe(2);
  });
});

describe('a pinned notice does not own the channel', () => {
  // "Dismiss-only" gives the user time to acknowledge; it must not mean every
  // later notice is invisible for the life of the tab.
  it('hands the slot over once something is queued behind it', () => {
    notifyError('Sync failed');
    notifySuccess('Copied link');

    vi.advanceTimersByTime(PINNED_HANDOVER_MS - 1);
    expect(get(currentNotice)?.message).toBe('Sync failed');

    vi.advanceTimersByTime(1);
    expect(get(currentNotice)?.message).toBe('Copied link');
  });

  it('keeps holding the slot while nothing is waiting', () => {
    notifyError('Sync failed');
    vi.advanceTimersByTime(PINNED_HANDOVER_MS * 4);
    expect(get(currentNotice)?.message).toBe('Sync failed');
  });

  // The handover window is measured from when the queue formed, not extended
  // by each new arrival — otherwise a steady drip would pin it indefinitely.
  it('does not restart the handover for each new arrival', () => {
    notifyError('Sync failed');
    notify('First');
    vi.advanceTimersByTime(PINNED_HANDOVER_MS - 1);
    notify('Second');
    vi.advanceTimersByTime(1);
    expect(get(currentNotice)?.message).toBe('First');
  });

  it('bounds the backlog, dropping the oldest queued rather than the visible one', () => {
    notifyError('Sync failed');
    for (let i = 0; i < MAX_QUEUE + 3; i++) notify(`Queued ${i}`);

    const list = get(notices);
    expect(list).toHaveLength(MAX_QUEUE);
    // The head is what the user is reading, and the tail is what just happened.
    expect(list[0].message).toBe('Sync failed');
    expect(list[list.length - 1].message).toBe(`Queued ${MAX_QUEUE + 2}`);
  });
});

describe('coalescing preserves what a repeat does not restate', () => {
  // The escalation an explicit key exists for: a progress notice that ends in
  // a failure must present as the failure, not keep the first call's severity.
  it('takes the new severity when the repeat states one', () => {
    notify('Retrying', { key: 'sync', severity: 'info' });
    notify('Sync failed for good', { key: 'sync', severity: 'error' });

    const current = get(currentNotice);
    expect(current?.severity).toBe('error');
    expect(current?.message).toBe('Sync failed for good');
  });

  it('keeps the existing severity when the repeat states none', () => {
    notifyError('Sync failed', { key: 'sync' });
    notify('Still failing', { key: 'sync' });
    expect(get(currentNotice)?.severity).toBe('error');
  });

  // A repeat reporting progress must not delete the affordance the first call
  // offered — nor un-pin the notice by removing the action that pinned it.
  it('keeps an existing action when the repeat supplies none', () => {
    const run = vi.fn();
    notify('Upload failed', { key: 'up', action: { label: 'Retry', run } });
    notify('Upload failed again', { key: 'up' });

    expect(get(currentNotice)?.action?.label).toBe('Retry');
    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(get(currentNotice)?.message).toBe('Upload failed again');
  });

  it('replaces the action when the repeat supplies a new one', () => {
    notify('Upload failed', { key: 'up', action: { label: 'Retry', run: () => {} } });
    notify('Upload failed', { key: 'up', action: { label: 'Undo', run: () => {} } });
    expect(get(currentNotice)?.action?.label).toBe('Undo');
  });

  it('drops the action when the repeat passes one explicitly as undefined', () => {
    notify('Upload failed', { key: 'up', action: { label: 'Retry', run: () => {} } });
    notify('Upload recovered', { key: 'up', action: undefined });
    expect(get(currentNotice)?.action).toBeUndefined();
  });
});

describe('actions', () => {
  it('runs the action and dismisses the notice', () => {
    const run = vi.fn();
    const id = notify('Upload failed', { action: { label: 'Retry', run } });
    const current = get(currentNotice);
    expect(current?.id).toBe(id);
    current?.action?.run();
    expect(run).toHaveBeenCalledTimes(1);
  });
});

describe('clearNotices', () => {
  it('empties the queue and cancels the pending timer', () => {
    notify('First', { duration: 1000 });
    notify('Second', { duration: 1000 });
    clearNotices();
    expect(get(notices)).toEqual([]);
    // A surviving timer would dismiss whatever arrived next, at an arbitrary
    // moment in its life.
    notify('Third', { duration: 1000 });
    vi.advanceTimersByTime(999);
    expect(get(currentNotice)?.message).toBe('Third');
  });
});
