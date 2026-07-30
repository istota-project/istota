import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/svelte';
import NoticeDrawer from './NoticeDrawer.svelte';
import {
  notify,
  notifyError,
  clearNotices,
  dismissNotice,
  currentNotice,
} from '$lib/stores/notices';
import { get } from 'svelte/store';

/**
 * jsdom implements no Web Animations API, and Svelte drives a `css` transition
 * through `element.animate`. Without this the motion-enabled tests below can't
 * run at all — which is exactly how the retract window went unexercised.
 */
const realAnimate = (Element.prototype as unknown as { animate?: unknown }).animate;

function stubWebAnimations() {
  (Element.prototype as unknown as { animate: unknown }).animate = function (
    _keyframes: unknown,
    options: { duration?: number } = {},
  ) {
    const ms = typeof options.duration === 'number' ? options.duration : 0;
    const animation: Record<string, unknown> = {
      currentTime: 0,
      playState: 'running',
      effect: null,
      onfinish: null,
      cancel() {
        animation.playState = 'idle';
      },
    };
    setTimeout(() => {
      if (animation.playState === 'idle') return;
      animation.currentTime = ms;
      animation.playState = 'finished';
      (animation.onfinish as (() => void) | null)?.();
    }, ms);
    return animation;
  };
}

function setReducedMotion(reduced: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: reduced && query.includes('prefers-reduced-motion'),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}

// Most tests reduce motion, which collapses the slide to zero duration so the
// panel leaves the DOM promptly and assertions don't wait out an animation.
// The tests that care about the animation window opt back in.
beforeEach(() => {
  setReducedMotion(true);
  stubWebAnimations();
  clearNotices();
});

afterEach(() => {
  cleanup();
  clearNotices();
  (Element.prototype as unknown as { animate?: unknown }).animate = realAnimate;
});

describe('NoticeDrawer', () => {
  it('renders nothing until a notice is raised', () => {
    render(NoticeDrawer);
    expect(screen.queryByTestId('notice-panel')).toBeNull();
  });

  it('shows the message once one is raised', async () => {
    render(NoticeDrawer);
    notify('Share link copied');
    expect(await screen.findByText('Share link copied')).not.toBeNull();
  });

  it('retracts when the notice is dismissed', async () => {
    render(NoticeDrawer);
    notify('Share link copied');
    await screen.findByText('Share link copied');

    await fireEvent.click(screen.getByLabelText('Dismiss notification'));
    await waitFor(() => expect(screen.queryByTestId('notice-panel')).toBeNull());
  });

  it('shows the next queued notice after the first is dismissed', async () => {
    render(NoticeDrawer);
    notify('First');
    notify('Second');
    await screen.findByText('First');

    await fireEvent.click(screen.getByLabelText('Dismiss notification'));
    expect(await screen.findByText('Second')).not.toBeNull();
  });
});

describe('severity presentation', () => {
  // Colour alone cannot carry the severity — an icon differs per severity for
  // sighted users, and a hidden word carries it to a screen reader.
  it('names the severity in text for assistive tech', async () => {
    render(NoticeDrawer);
    notifyError('Sync failed');
    expect(await screen.findByText('Error:')).not.toBeNull();
  });

  it('marks the panel with its severity', async () => {
    render(NoticeDrawer);
    notify('Heads up', { severity: 'warning' });
    const panel = await screen.findByTestId('notice-panel');
    expect(panel.getAttribute('data-severity')).toBe('warning');
  });

  it('carries a severity icon that is hidden from the accessibility tree', async () => {
    render(NoticeDrawer);
    notify('All good', { severity: 'success' });
    await screen.findByText('All good');
    expect(screen.getByTestId('notice-icon').getAttribute('aria-hidden')).toBe('true');
  });
});

describe('live regions', () => {
  // Both regions are mounted for the page's whole life and neither changes its
  // politeness. A region only announces reliably if assistive tech had it
  // registered, at that politeness, before the text arrived — so routing the
  // message between two fixed regions is not the same as flipping one.
  it('mounts both regions, empty, before any notice', () => {
    render(NoticeDrawer);
    expect(screen.getByTestId('notice-announce-polite').textContent).toBe('');
    expect(screen.getByTestId('notice-announce-assertive').textContent).toBe('');
  });

  it('announces an error in the assertive region only', async () => {
    render(NoticeDrawer);
    notifyError('Sync failed');
    await screen.findByText('Sync failed');
    expect(screen.getByTestId('notice-announce-assertive').textContent).toBe('Error: Sync failed');
    expect(screen.getByTestId('notice-announce-polite').textContent).toBe('');
  });

  it('announces everything else in the polite region only', async () => {
    render(NoticeDrawer);
    notify('Saved', { severity: 'success' });
    await screen.findByText('Saved');
    expect(screen.getByTestId('notice-announce-polite').textContent).toBe('Success: Saved');
    expect(screen.getByTestId('notice-announce-assertive').textContent).toBe('');
  });

  // A burst that collapses to one visible band must collapse to one spoken
  // announcement too — which it does only if the text stays identical, so the
  // count must stay out of it.
  it('leaves the repeat count out of the announcement', async () => {
    render(NoticeDrawer);
    notifyError('Network unreachable');
    notifyError('Network unreachable');
    await screen.findByText('×2');
    expect(screen.getByTestId('notice-announce-assertive').textContent).toBe(
      'Error: Network unreachable',
    );
  });

  it('empties the region once the notice is gone', async () => {
    render(NoticeDrawer);
    const id = notify('Saved');
    await screen.findByText('Saved');
    dismissNotice(id);
    await waitFor(() => expect(screen.getByTestId('notice-announce-polite').textContent).toBe(''));
  });
});

describe('the retract window', () => {
  // Svelte keeps a block's listeners live through its outro, so for the whole
  // animation the panel is on screen, clickable, and backed by a store value
  // that has already gone null. A handler reading the reactive value there
  // throws; one that captured it at render time does not.
  it('survives a second dismiss during the slide back up', async () => {
    setReducedMotion(false);
    render(NoticeDrawer);
    notify('Saved');
    await screen.findByText('Saved');

    const dismiss = screen.getByLabelText('Dismiss notification');
    await fireEvent.click(dismiss);
    // Still in the DOM, mid-outro, with the store already empty.
    expect(get(currentNotice)).toBeNull();
    await fireEvent.click(dismiss);

    await waitFor(() => expect(screen.queryByTestId('notice-panel')).toBeNull());
  });

  it('survives the action being taken twice during the slide back up', async () => {
    setReducedMotion(false);
    const run = vi.fn();
    render(NoticeDrawer);
    notify('Upload failed', { action: { label: 'Retry', run } });
    const button = await screen.findByText('Retry');

    await fireEvent.click(button);
    await fireEvent.click(button);

    // The second click lands on a panel whose notice is gone; it must not throw,
    // and it must not run the action again.
    expect(run).toHaveBeenCalledTimes(1);
  });
});

describe('reduced motion', () => {
  it('removes the panel immediately when motion is reduced', async () => {
    setReducedMotion(true);
    render(NoticeDrawer);
    const id = notify('Saved');
    await screen.findByText('Saved');
    dismissNotice(id);
    await waitFor(() => expect(screen.queryByTestId('notice-panel')).toBeNull());
  });

  // The counterpart: with motion allowed the panel outlives its dismissal for
  // the length of the slide, which is what makes the capture above necessary.
  it('keeps the panel through the slide when motion is allowed', async () => {
    setReducedMotion(false);
    render(NoticeDrawer);
    const id = notify('Saved');
    await screen.findByText('Saved');
    dismissNotice(id);
    expect(screen.queryByTestId('notice-panel')).not.toBeNull();
    await waitFor(() => expect(screen.queryByTestId('notice-panel')).toBeNull());
  });
});

describe('action', () => {
  it('renders the action label and runs it', async () => {
    const run = vi.fn();
    render(NoticeDrawer);
    notify('Upload failed', { action: { label: 'Retry', run } });

    await fireEvent.click(await screen.findByText('Retry'));
    expect(run).toHaveBeenCalledTimes(1);
  });

  // Taking the action answers the notice, so leaving it on screen would invite
  // a second press of a button whose work is already under way.
  it('dismisses the notice once the action is taken', async () => {
    render(NoticeDrawer);
    notify('Upload failed', { action: { label: 'Retry', run: () => {} } });

    await fireEvent.click(await screen.findByText('Retry'));
    await waitFor(() => expect(get(currentNotice)).toBeNull());
  });

  it('renders no action button when the notice carries none', async () => {
    render(NoticeDrawer);
    notify('Saved');
    await screen.findByText('Saved');
    expect(screen.queryByTestId('notice-action')).toBeNull();
  });
});

describe('coalesced repeats', () => {
  it('shows a count once the same notice repeats', async () => {
    render(NoticeDrawer);
    notifyError('Network unreachable');
    notifyError('Network unreachable');
    expect(await screen.findByText('×2')).not.toBeNull();
  });

  it('shows no count for a notice raised once', async () => {
    render(NoticeDrawer);
    notifyError('Network unreachable');
    await screen.findByText('Network unreachable');
    expect(screen.queryByTestId('notice-count')).toBeNull();
  });
});
