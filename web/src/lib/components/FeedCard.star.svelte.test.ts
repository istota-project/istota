/**
 * Starring a feed entry is an optimistic update: the star flips immediately and
 * is put back if the server refuses. The rollback is indistinguishable from a
 * mistap — the star simply springs back — so a refusal has to say so, which is
 * what routes it through the notice layer.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { FeedEntry } from '$lib/api';

const api = vi.hoisted(() => ({ updateEntryStarred: vi.fn() }));
vi.mock('$lib/api', () => api);

import FeedCard from './FeedCard.svelte';
import { currentNotice, clearNotices } from '$lib/stores/notices';

beforeEach(() => {
  api.updateEntryStarred.mockReset();
  clearNotices();
});

afterEach(() => {
  cleanup();
  clearNotices();
});

function entry(over: Partial<FeedEntry> = {}): FeedEntry {
  return {
    id: 1,
    title: 'A post title',
    url: 'https://example.com/post',
    content: '<p>Body copy.</p>',
    images: [],
    duplicate_image_count: 0,
    embed_url: '',
    file_url: '',
    feed: {
      id: 1,
      title: 'Example Feed',
      site_url: 'https://example.com',
      category: null as never,
    },
    published_at: '2026-07-01T10:00:00Z',
    starred: false,
    starred_at: '',
    read: false,
    ...over,
  } as FeedEntry;
}

describe('FeedCard star failure', () => {
  it('raises a notice when the server refuses the star', async () => {
    api.updateEntryStarred.mockRejectedValue(new Error('boom'));
    render(FeedCard, { entry: entry(), onImageClick: () => {} });

    await fireEvent.click(screen.getByLabelText('Star entry'));

    expect(get(currentNotice)?.message).toBe("Couldn't update star.");
    expect(get(currentNotice)?.severity).toBe('error');
  });

  it('says nothing when the star succeeds', async () => {
    api.updateEntryStarred.mockResolvedValue(undefined);
    render(FeedCard, { entry: entry(), onImageClick: () => {} });

    await fireEvent.click(screen.getByLabelText('Star entry'));

    expect(get(currentNotice)).toBeNull();
  });

  // A burst of failures against a flaky backend is one notice with a count, not
  // one band per entry the user touched.
  it('coalesces repeated failures into a single counted notice', async () => {
    api.updateEntryStarred.mockRejectedValue(new Error('boom'));
    render(FeedCard, { entry: entry(), onImageClick: () => {} });

    const button = screen.getByLabelText('Star entry');
    await fireEvent.click(button);
    await fireEvent.click(screen.getByLabelText('Star entry'));

    expect(get(currentNotice)?.count).toBe(2);
  });
});
