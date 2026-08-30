/**
 * What the briefings reader says when the archive could not be fetched.
 *
 * The reported bug: offline, every other module said "Failed to load …" and
 * briefings said "No briefings yet. Once a scheduled briefing runs it will
 * appear here." — telling a user with configured briefings that they have never
 * had one, and pointing them at settings to fix a problem that was the network.
 *
 * The mechanism is worth stating, because the swallow looked deliberate. The
 * layout owns the archive fetch and caught its failure with a comment saying
 * "the reader page surfaces its own load errors". The reader cannot: it only
 * fetches the *selected* briefing, and on a failed list fetch there is no
 * selection to fetch, so its effect returns before it can set an error. The
 * layout then published `briefingArchiveCount = 0`, which is the store's
 * spelling of "loaded, and there is nothing", so the reader rendered the empty
 * state exactly as designed. Two correct-looking halves, one wrong answer.
 *
 * The third case here is the control, and it is the one that stops the fix
 * being "always show an error": an archive that really is empty must still read
 * as empty.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';

vi.mock('$lib/api', () => ({
  getBriefingArchive: vi.fn(),
  deleteBriefingArchiveItem: vi.fn(),
  getBriefingArchiveItem: vi.fn(),
}));

import { getBriefingArchive } from '$lib/api';
import {
  briefingArchiveCount,
  briefingArchiveError,
  selectedBriefingId,
  briefingFilterName,
} from '$lib/stores/briefings';
import Layout from './+layout.svelte';
import Page from './+page.svelte';

const children = (() => {}) as unknown as import('svelte').Snippet;

const EMPTY = { items: [], total: 0, briefing_names: [] };

beforeEach(() => {
  vi.clearAllMocks();
  briefingArchiveCount.set(null);
  briefingArchiveError.set(null);
  selectedBriefingId.set(null);
  briefingFilterName.set('');
});

afterEach(cleanup);

describe('the layout, whose fetch it is', () => {
  it('publishes the failure instead of swallowing it', async () => {
    vi.mocked(getBriefingArchive).mockRejectedValue(new TypeError('Failed to fetch'));

    render(Layout, { children });
    await vi.waitFor(() => expect(get(briefingArchiveError)).toBe('Failed to load briefings'));
  });

  it('clears it once the archive loads again', async () => {
    vi.mocked(getBriefingArchive).mockRejectedValue(new TypeError('Failed to fetch'));
    render(Layout, { children });
    await vi.waitFor(() => expect(get(briefingArchiveError)).toBeTruthy());
    cleanup();

    vi.mocked(getBriefingArchive).mockResolvedValue(EMPTY);
    render(Layout, { children });
    await vi.waitFor(() => expect(get(briefingArchiveError)).toBeNull());
  });

  it('does not tell the sidebar there are no briefings', async () => {
    vi.mocked(getBriefingArchive).mockRejectedValue(new TypeError('Failed to fetch'));

    render(Layout, { children });
    await vi.waitFor(() => expect(get(briefingArchiveError)).toBeTruthy());
    expect(screen.queryByText('No briefings yet.')).toBeNull();
  });
});

describe('the reader pane', () => {
  it('reports the failure rather than an empty archive', async () => {
    briefingArchiveCount.set(0);
    briefingArchiveError.set('Failed to load briefings');

    render(Page);

    expect(await screen.findByText('Failed to load briefings')).toBeTruthy();
    expect(screen.queryByText('No briefings yet')).toBeNull();
  });

  it('renders that failure in the shared whole-pane box', async () => {
    briefingArchiveCount.set(0);
    briefingArchiveError.set('Failed to load briefings');

    render(Page);

    // The same class every other module's load failure uses, so this one is
    // centred and sized with them rather than being a third spelling.
    const el = await screen.findByText('Failed to load briefings');
    expect(el.className).toContain('center-msg');
    expect(el.className).toContain('error');
  });

  it('still reads as empty when the archive really is empty', async () => {
    briefingArchiveCount.set(0);
    briefingArchiveError.set(null);

    render(Page);

    expect(await screen.findByText('No briefings yet')).toBeTruthy();
  });
});
