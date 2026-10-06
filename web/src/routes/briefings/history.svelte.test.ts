import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import { goto } from '$app/navigation';
import { __history } from '../../../vitest-stubs/app-navigation';
import { page } from '../../../vitest-stubs/app-state.svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$app/navigation', async (original) => ({
  ...(await original<typeof import('$app/navigation')>()),
  goto: vi.fn(async () => undefined),
}));
await fillApiDouble(api);

import {
  getBriefingArchive,
  getBriefingArchiveItem,
  type BriefingArchiveItem,
  type User,
} from '$lib/api';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import {
  selectedBriefingId,
  briefingFilterName,
  briefingArchiveCount,
  briefingArchiveError,
  briefingsRefreshNonce,
} from '$lib/stores/briefings';
import Layout from './+layout.svelte';

const children = (() => {}) as unknown as import('svelte').Snippet;
const items: BriefingArchiveItem[] = [
  { id: 3, briefing_name: 'Evening', subject: 'Evening news' },
  { id: 2, briefing_name: 'Morning', subject: 'Morning news' },
  { id: 1, briefing_name: 'Morning', subject: 'Earlier news' },
].map((item) => ({
  ...item,
  generated_at: '2026-01-01T12:00:00Z',
  task_id: null,
  delivered_to: [],
}));
const currentUrl = () => __history.entries[__history.index].url;
const archive = (name = '') => {
  const filtered = items.filter((item) => !name || item.briefing_name === name);
  return { items: filtered, total: filtered.length, briefing_names: ['Morning', 'Evening'] };
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getBriefingArchiveItem).mockImplementation(async (id) => {
    const item = items.find((item) => item.id === id);
    if (!item) throw Object.assign(new Error('briefing not found'), { status: 404 });
    return item;
  });
  __history.reset('/istota/briefings/');
  selectedBriefingId.set(null);
  briefingFilterName.set('');
  briefingArchiveCount.set(null);
  briefingArchiveError.set(null);
  briefingsRefreshNonce.set(0);
  vi.mocked(getBriefingArchive).mockImplementation(async (params) =>
    archive(params?.briefing_name),
  );
});
afterEach(cleanup);

async function openReader() {
  render(Layout, { children });
  await waitFor(() => expect(get(briefingArchiveCount)).toBe(3));
  await tick();
}

async function chooseName(name: string) {
  const trigger = screen.getByRole('button', { name: 'Filter by briefing' });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const option = await screen.findByRole('option', { name });
  await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(option);
}

describe('briefing selection history', () => {
  it('pushes an item once, then restores the reader on Back and Forward', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: /Earlier news/ }));
    expect(currentUrl()).toBe('/istota/briefings/?id=1');
    expect(__history.entries).toHaveLength(2);
    await fireEvent.click(screen.getByRole('button', { name: /Earlier news/ }));
    expect(__history.entries).toHaveLength(2);
    __history.back();
    await waitFor(() => expect(get(selectedBriefingId)).toBe(3));
    __history.forward();
    await waitFor(() => expect(get(selectedBriefingId)).toBe(1));
    expect(__history.entries).toHaveLength(2);
  });

  it('pushes a name filter and keeps it when selecting an item', async () => {
    await openReader();
    await chooseName('Morning');
    await waitFor(() => expect(currentUrl()).toBe('/istota/briefings/?id=2&name=Morning'));
    await fireEvent.click(screen.getByRole('button', { name: /Earlier news/ }));
    expect(currentUrl()).toBe('/istota/briefings/?id=1&name=Morning');
    expect(__history.entries).toHaveLength(3);
    __history.back();
    await waitFor(() => expect(get(selectedBriefingId)).toBe(2));
    __history.back();
    await waitFor(() => expect(get(briefingFilterName)).toBe(''));
    await waitFor(() => expect(get(briefingArchiveCount)).toBe(3));
    expect(get(selectedBriefingId)).toBe(3);
  });

  it('restores a reload selection before the archive request resolves', async () => {
    __history.reset('/istota/briefings/?id=1&name=Morning');
    let finish!: (value: ReturnType<typeof archive>) => void;
    vi.mocked(getBriefingArchive).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(Layout, { children });
    await waitFor(() =>
      expect(getBriefingArchive).toHaveBeenCalledWith(
        expect.objectContaining({ briefing_name: 'Morning' }),
      ),
    );
    expect(currentUrl()).toBe('/istota/briefings/?id=1&name=Morning');
    finish(archive('Morning'));
    await waitFor(() => expect(get(briefingArchiveCount)).toBe(2));
    expect(get(selectedBriefingId)).toBe(1);
    expect(__history.entries).toHaveLength(1);
  });

  it.each(['999', '-1', '1.5'])(
    'replaces a missing or invalid id %s with the first item',
    async (id) => {
      __history.reset(`/istota/briefings/?id=${id}`);
      await openReader();
      expect(currentUrl()).toBe('/istota/briefings/?id=3');
      expect(__history.entries).toHaveLength(1);
    },
  );

  it('clears a missing name and reloads All before selecting its first item', async () => {
    __history.reset('/istota/briefings/?id=999&name=Retired');
    await openReader();
    expect(get(briefingFilterName)).toBe('');
    expect(currentUrl()).toBe('/istota/briefings/?id=3');
    expect(getBriefingArchive).toHaveBeenLastCalledWith({ limit: '20', offset: '0' });
    expect(__history.entries).toHaveLength(1);
  });

  it('ignores a filter response that arrives after Back restored All', async () => {
    await openReader();
    let finish!: (value: ReturnType<typeof archive>) => void;
    vi.mocked(getBriefingArchive).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await chooseName('Morning');
    expect(currentUrl()).toBe('/istota/briefings/?name=Morning');
    __history.back();
    await waitFor(() => expect(get(briefingFilterName)).toBe(''));
    await waitFor(() => expect(get(selectedBriefingId)).toBe(3));
    finish(archive('Morning'));
    await tick();
    expect(get(briefingArchiveCount)).toBe(3);
    expect(currentUrl()).toBe('/istota/briefings/?id=3');
  });

  it('replaces Back onto an item removed from the archive', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: /Earlier news/ }));
    vi.mocked(getBriefingArchive).mockResolvedValue({
      ...archive(),
      items: items.slice(1),
      total: 2,
    });
    vi.mocked(getBriefingArchiveItem).mockRejectedValue(
      Object.assign(new Error('briefing not found'), { status: 404 }),
    );
    briefingsRefreshNonce.update((n) => n + 1);
    await waitFor(() => expect(get(briefingArchiveCount)).toBe(2));
    __history.back();
    await waitFor(() => expect(currentUrl()).toBe('/istota/briefings/?id=2'));
    expect(get(selectedBriefingId)).toBe(2);
    expect(__history.entries).toHaveLength(2);
  });

  it('restores an older briefing outside the first archive page in the real reader', async () => {
    const firstPage = Array.from({ length: 20 }, (_, index) => ({ ...items[0], id: index + 10 }));
    const older = { ...items[2], id: 30, subject: 'An older briefing' };
    __history.reset('/istota/briefings/?id=30');
    vi.mocked(getBriefingArchive).mockResolvedValue({
      items: firstPage,
      total: 21,
      briefing_names: ['Morning', 'Evening'],
    });
    vi.mocked(getBriefingArchiveItem).mockResolvedValue(older);
    render(Harness, { layout: Layout, component: Page, user: { username: 'alice' } as User });
    await screen.findByRole('heading', { name: 'An older briefing' });
    await waitFor(() => expect(get(briefingArchiveCount)).toBe(20));
    await tick();
    expect(get(selectedBriefingId)).toBe(30);
    expect(currentUrl()).toBe('/istota/briefings/?id=30');
    expect(__history.entries).toHaveLength(1);
  });

  it('falls back when an unloaded briefing belongs to a different name', async () => {
    __history.reset('/istota/briefings/?id=3&name=Morning');
    render(Layout, { children });
    await waitFor(() => expect(currentUrl()).toBe('/istota/briefings/?id=2&name=Morning'));
    expect(get(selectedBriefingId)).toBe(2);
    expect(getBriefingArchiveItem).toHaveBeenCalledWith(3);
  });

  it('keeps an unresolved id and reports a transient detail lookup failure', async () => {
    __history.reset('/istota/briefings/?id=30');
    vi.mocked(getBriefingArchiveItem).mockRejectedValue(new TypeError('Failed to fetch'));
    render(Layout, { children });
    await waitFor(() => expect(get(briefingArchiveError)).toBe('Failed to load briefings'));
    expect(get(selectedBriefingId)).toBe(30);
    expect(currentUrl()).toBe('/istota/briefings/?id=30');
  });

  it('does not let a late missing-id error replace the successful fallback reader', async () => {
    __history.reset('/istota/briefings/?id=999');
    let rejectMissing!: (error: Error) => void;
    let finishArchive!: (value: ReturnType<typeof archive>) => void;
    vi.mocked(getBriefingArchive).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishArchive = resolve;
        }),
    );
    let missingCalls = 0;
    vi.mocked(getBriefingArchiveItem).mockImplementation((id) => {
      if (id !== 999) return Promise.resolve(items[0]);
      if (missingCalls++ === 0)
        return new Promise((_resolve, reject) => {
          rejectMissing = reject;
        });
      return Promise.reject(Object.assign(new Error('briefing not found'), { status: 404 }));
    });
    render(Harness, { layout: Layout, component: Page, user: { username: 'alice' } as User });
    await waitFor(() => expect(getBriefingArchiveItem).toHaveBeenCalledWith(999));
    finishArchive(archive());
    await screen.findByRole('heading', { name: 'Evening news' });
    rejectMissing(Object.assign(new Error('briefing not found'), { status: 404 }));
    await tick();
    await tick();
    expect(screen.queryByText('briefing not found')).toBeNull();
    expect(screen.getByRole('heading', { name: 'Evening news' })).toBeTruthy();
  });

  it('ignores a reader failure after the selection is cleared', async () => {
    selectedBriefingId.set(999);
    briefingArchiveCount.set(0);
    let rejectMissing!: (error: Error) => void;
    vi.mocked(getBriefingArchiveItem).mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectMissing = reject;
        }),
    );
    render(Page);
    await waitFor(() => expect(getBriefingArchiveItem).toHaveBeenCalledWith(999));
    selectedBriefingId.set(null);
    await tick();
    rejectMissing(new Error('briefing not found'));
    await tick();
    await tick();
    expect(screen.queryByText('briefing not found')).toBeNull();
    expect(screen.getByRole('heading', { name: 'No briefings yet' })).toBeTruthy();
  });

  it('carries the current selection back from settings without writing its URL', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: /Earlier news/ }));
    await fireEvent.click(screen.getByTitle('Briefing settings'));
    expect(goto).toHaveBeenLastCalledWith('/istota/briefings/settings');
    __history.reset('/istota/briefings/settings/');
    await tick();
    selectedBriefingId.set(2);
    await tick();
    expect(currentUrl()).toBe('/istota/briefings/settings/');
    await fireEvent.click(screen.getByTitle('Briefing settings'));
    expect(goto).toHaveBeenLastCalledWith('/istota/briefings/?id=2');
    expect(__history.entries).toHaveLength(1);
  });

  it('applies a new reader query when the layout survives settings navigation', async () => {
    __history.reset('/istota/briefings/settings/');
    render(Layout, { children });
    await waitFor(() => expect(get(briefingArchiveCount)).toBe(3));
    window.history.replaceState(null, '', '/istota/briefings/?id=1&name=Morning');
    page.url = new URL(window.location.href);
    page.state = {};
    await waitFor(() => expect(get(selectedBriefingId)).toBe(1));
    await waitFor(() => expect(get(briefingArchiveCount)).toBe(2));
    expect(get(briefingFilterName)).toBe('Morning');
    expect(window.location.search).toBe('?id=1&name=Morning');
  });
});
