import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { BriefingArchiveItem, User } from '$lib/api';
import Harness from '$lib/currentUserHarness.test.svelte';
import { __history } from '../../../vitest-stubs/app-navigation';
import {
  selectedBriefingId,
  briefingFilterName,
  briefingArchiveCount,
  briefingArchiveError,
} from '$lib/stores/briefings';
import Page from './+page.svelte';
import Layout from './+layout.svelte';

vi.mock('$app/paths', () => ({ base: '/istota' }));

const fetchMock = vi.fn();
const readAt = '2026-01-02T12:00:00Z';
let visibility: DocumentVisibilityState;
let loadBody: (id: number) => Promise<Response>;
let acknowledge: (id: number) => Promise<Response>;

function item(id: number): BriefingArchiveItem {
  return {
    id,
    briefing_name: 'Morning',
    subject: `Briefing ${id}`,
    body_md: `Body of briefing ${id}`,
    generated_at: '2026-01-01T12:00:00Z',
    task_id: null,
    delivered_to: [],
    read_at: null,
  };
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function readRequests() {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
}

async function show(state: DocumentVisibilityState) {
  visibility = state;
  document.dispatchEvent(new Event('visibilitychange'));
  await settle();
}

async function settle() {
  // Drain the HTTP, selection-effect and render-tick continuations.
  for (let i = 0; i < 8; i++) await tick();
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  visibility = 'visible';
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
  __history.reset('/istota/briefings/');
  selectedBriefingId.set(null);
  briefingFilterName.set('');
  briefingArchiveCount.set(2);
  briefingArchiveError.set(null);
  loadBody = async (id) => response(item(id));
  acknowledge = async (id) => response({ id, read_at: readAt });
  fetchMock.mockImplementation(async (url: string, init: RequestInit) => {
    const match = url.match(/\/archive\/(\d+)(\/read)?$/);
    if (match) {
      const id = Number(match[1]);
      return match[2] ? acknowledge(id) : loadBody(id);
    }
    if (url.startsWith('/istota/api/briefings/archive?')) {
      return response({ items: [item(2), item(1)], total: 2, briefing_names: ['Morning'] });
    }
    throw new Error(`Unexpected request: ${init?.method ?? 'GET'} ${url}`);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('briefing reader acknowledgments', () => {
  it('posts only after the automatically selected body renders, with badges off', async () => {
    let bodyRenderedAtAcknowledgment = false;
    acknowledge = async (id) => {
      bodyRenderedAtAcknowledgment = !!screen.queryByText(`Body of briefing ${id}`);
      return response({ id, read_at: readAt });
    };
    render(Harness, {
      layout: Layout,
      component: Page,
      user: { username: 'alice', show_nav_unread_badges: false } as User,
    });
    await waitFor(() => expect(readRequests()).toHaveLength(1));
    expect(bodyRenderedAtAcknowledgment).toBe(true);
    expect(readRequests()[0]).toEqual([
      '/istota/api/briefings/archive/2/read',
      { method: 'POST', credentials: 'same-origin' },
    ]);
  });

  it('does not acknowledge archive preloads on the settings route', async () => {
    __history.reset('/istota/briefings/settings/');
    render(Layout, { children: (() => {}) as unknown as import('svelte').Snippet });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await settle();
    expect(readRequests()).toHaveLength(0);
  });

  it('waits for a hidden document to become visible and acknowledges only once', async () => {
    visibility = 'hidden';
    selectedBriefingId.set(1);
    render(Page);
    await screen.findByText('Body of briefing 1');
    await settle();
    expect(readRequests()).toHaveLength(0);
    await show('visible');
    await waitFor(() => expect(readRequests()).toHaveLength(1));
    await show('hidden');
    await show('visible');
    expect(readRequests()).toHaveLength(1);
  });

  it('ignores a superseded body response', async () => {
    const oldBody = deferred<Response>();
    loadBody = async (id) => (id === 1 ? oldBody.promise : response(item(id)));
    selectedBriefingId.set(1);
    render(Page);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    selectedBriefingId.set(2);
    await waitFor(() => expect(readRequests()).toHaveLength(1));
    oldBody.resolve(response(item(1)));
    await settle();
    expect(screen.getByText('Body of briefing 2')).toBeTruthy();
    expect(screen.queryByText('Body of briefing 1')).toBeNull();
    expect(readRequests().map(([url]) => url)).toEqual(['/istota/api/briefings/archive/2/read']);
  });

  it('rechecks visibility after the body arrives and before acknowledging', async () => {
    loadBody = async (id) => {
      const result = response(item(id));
      const json = result.json.bind(result);
      result.json = async () => {
        const body = await json();
        visibility = 'hidden';
        document.dispatchEvent(new Event('visibilitychange'));
        return body;
      };
      return result;
    };
    selectedBriefingId.set(1);
    render(Page);
    await screen.findByText('Body of briefing 1');
    await settle();
    expect(readRequests()).toHaveLength(0);
    await show('visible');
    await waitFor(() => expect(readRequests()).toHaveLength(1));
  });

  it('rechecks selection when it changes at the render boundary', async () => {
    const observer = new MutationObserver(() => {
      if (screen.queryByText('Body of briefing 1')) selectedBriefingId.set(2);
    });
    observer.observe(document.body, { childList: true, subtree: true });
    selectedBriefingId.set(1);
    try {
      render(Page);
      await screen.findByText('Body of briefing 2');
      await waitFor(() => expect(readRequests()).toHaveLength(1));
      expect(readRequests()[0][0]).toBe('/istota/api/briefings/archive/2/read');
    } finally {
      observer.disconnect();
    }
  });

  it('does not mark the previous body while a new selection is loading', async () => {
    visibility = 'hidden';
    const pending = deferred<Response>();
    loadBody = async (id) => (id === 2 ? pending.promise : response(item(id)));
    selectedBriefingId.set(1);
    render(Page);
    await screen.findByText('Body of briefing 1');
    selectedBriefingId.set(2);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await show('visible');
    expect(readRequests()).toHaveLength(0);
    pending.resolve(response(item(2)));
    await waitFor(() => expect(readRequests()).toHaveLength(1));
    expect(readRequests()[0][0]).toBe('/istota/api/briefings/archive/2/read');
  });

  it('does not acknowledge a failed body fetch', async () => {
    loadBody = async () => response({ detail: 'Body unavailable' }, 500);
    selectedBriefingId.set(1);
    render(Page);
    await screen.findByText('Body unavailable');
    await show('hidden');
    await show('visible');
    expect(readRequests()).toHaveLength(0);
  });

  it('keeps the article readable on read failure and retries only on request', async () => {
    acknowledge = async () => response({ detail: 'Write unavailable' }, 500);
    selectedBriefingId.set(1);
    render(Page);
    await screen.findByText('Could not mark this briefing as read.');
    expect(screen.getByText('Body of briefing 1')).toBeTruthy();
    await show('visible');
    briefingArchiveCount.set(3);
    await settle();
    expect(readRequests()).toHaveLength(1);
    acknowledge = async (id) => response({ id, read_at: readAt });
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(readRequests()).toHaveLength(2));
    await waitFor(() =>
      expect(screen.queryByText('Could not mark this briefing as read.')).toBeNull(),
    );
    await show('hidden');
    await show('visible');
    expect(readRequests()).toHaveLength(2);
  });

  it('allows a failed acknowledgment to retry on a new visible presentation', async () => {
    acknowledge = async () => response({}, 500);
    selectedBriefingId.set(1);
    render(Page);
    await screen.findByText('Could not mark this briefing as read.');
    acknowledge = async (id) => response({ id, read_at: readAt });
    await show('hidden');
    await show('visible');
    await waitFor(() => expect(readRequests()).toHaveLength(2));
  });

  it('suppresses duplicate pending acknowledgments across reselection', async () => {
    const pending = deferred<Response>();
    acknowledge = async () => pending.promise;
    selectedBriefingId.set(1);
    render(Page);
    await waitFor(() => expect(readRequests()).toHaveLength(1));
    selectedBriefingId.set(null);
    await settle();
    selectedBriefingId.set(1);
    await screen.findByText('Body of briefing 1');
    await show('hidden');
    await show('visible');
    expect(readRequests()).toHaveLength(1);
    pending.resolve(response({ id: 1, read_at: readAt }));
    await settle();
    await show('hidden');
    await show('visible');
    expect(readRequests()).toHaveLength(1);
  });

  it.each([200, 500])(
    'a late read response (%s) does not change the newer article',
    async (status) => {
      const pending = deferred<Response>();
      acknowledge = async (id) => (id === 1 ? pending.promise : response({ id, read_at: readAt }));
      selectedBriefingId.set(1);
      render(Page);
      await waitFor(() => expect(readRequests()).toHaveLength(1));
      selectedBriefingId.set(2);
      await waitFor(() => expect(readRequests()).toHaveLength(2));
      pending.resolve(response({ id: 1, read_at: readAt }, status));
      await settle();
      expect(screen.getByText('Body of briefing 2')).toBeTruthy();
      expect(screen.queryByText('Could not mark this briefing as read.')).toBeNull();
    },
  );

  it('skips an entry that was already read', async () => {
    loadBody = async (id) => response({ ...item(id), read_at: readAt });
    selectedBriefingId.set(1);
    render(Page);
    await screen.findByText('Body of briefing 1');
    await show('hidden');
    await show('visible');
    expect(readRequests()).toHaveLength(0);
  });

  it('does not acknowledge a body that arrives after leaving the reader', async () => {
    const pending = deferred<Response>();
    loadBody = async () => pending.promise;
    selectedBriefingId.set(1);
    const view = render(Page);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    view.unmount();
    pending.resolve(response(item(1)));
    await show('hidden');
    await show('visible');
    expect(readRequests()).toHaveLength(0);
  });
});
