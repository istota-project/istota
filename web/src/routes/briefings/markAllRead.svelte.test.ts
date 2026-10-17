import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import type { BriefingArchiveItem, User } from '$lib/api';
import Harness from '$lib/currentUserHarness.test.svelte';
import { __history } from '../../../vitest-stubs/app-navigation';
import {
  selectedBriefingId,
  briefingFilterName,
  briefingArchiveCount,
  briefingArchiveError,
  briefingsRefreshNonce,
} from '$lib/stores/briefings';
import { navUnreadCounts, startNavUnreadPoll, stopNavUnreadPoll } from '$lib/stores/navUnread';
import { clearNotices } from '$lib/stores/notices';
import Page from './+page.svelte';
import Layout from './+layout.svelte';
import HeaderSavePage from './headerSave.test.svelte';

vi.mock('$app/paths', () => ({ base: '/istota' }));

const fetchMock = vi.fn();
const allLabel = 'Mark all briefings as read';
const nameLabel = "Mark this briefing's archive as read";
const named = 'Morning <news>';
let bulkResponse: () => Promise<Response>;
let archive: BriefingArchiveItem[];

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function bulkRequests() {
  return fetchMock.mock.calls.filter(([url]) => url.endsWith('/briefings/mark-as-read'));
}

async function settle() {
  for (let i = 0; i < 8; i++) await tick();
}

async function openReader(badges = false, name = '') {
  __history.reset(`/istota/briefings/${name ? `?name=${encodeURIComponent(name)}` : ''}`);
  const view = render(Harness, {
    layout: Layout,
    component: Page,
    user: { username: 'alice', show_nav_unread_badges: badges } as User,
  });
  await screen.findByText('Body of briefing 1');
  await settle();
  return view;
}

async function confirm() {
  await fireEvent.click(screen.getByRole('button', { name: 'Mark all read' }));
  await settle();
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  selectedBriefingId.set(null);
  briefingFilterName.set('');
  briefingArchiveCount.set(null);
  briefingArchiveError.set(null);
  briefingsRefreshNonce.set(0);
  archive = Array.from({ length: 45 }, (_, index) => ({
    id: index + 1,
    briefing_name: index < 42 ? named : 'Evening',
    subject: `Briefing ${index + 1}`,
    body_md: `Body of briefing ${index + 1}`,
    generated_at: '2026-01-01T12:00:00Z',
    task_id: null,
    delivered_to: [],
    read_at: '2026-01-02T12:00:00Z',
  }));
  bulkResponse = async () => response({ status: 'ok', updated: 45 });
  fetchMock.mockImplementation(async (url: string) => {
    if (url.endsWith('/briefings/mark-as-read')) return bulkResponse();
    if (url.includes('/briefings/archive?')) {
      const params = new URL(url, 'http://localhost').searchParams;
      const name = params.get('briefing_name');
      const filtered = archive.filter((item) => !name || item.briefing_name === name);
      const offset = Number(params.get('offset'));
      return response({
        items: filtered.slice(offset, offset + 20),
        total: filtered.length,
        briefing_names: [named, 'Evening'],
      });
    }
    const match = url.match(/\/archive\/(\d+)$/);
    if (match) return response(archive.find((item) => item.id === Number(match[1])));
    throw new Error(`Unexpected request: ${url}`);
  });
});

afterEach(() => {
  cleanup();
  stopNavUnreadPoll();
  clearNotices();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('briefing bulk read in the real layout', () => {
  it.each([false, true])('uses the header icon control with badges %s', async (badges) => {
    await openReader(badges);
    const button = screen.getByRole('button', { name: allLabel });
    expect(button.getAttribute('title')).toBe(allLabel);
    expect(button.classList.contains('chip')).toBe(true);
    expect(button.classList.contains('icon')).toBe(true);
    expect(button.querySelector('svg')?.getAttribute('width')).toBe('14');
    const tools = button.closest('.header-tools') as HTMLElement;
    expect(
      within(tools)
        .getAllByRole('button')
        .map((el) => el.title),
    ).toEqual(['New briefing', allLabel, 'Briefing settings']);
    await fireEvent.click(button);
    expect(
      screen.getByText(
        "Are you sure you want to mark all unread briefings as read? This can't be undone.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole('button', { name: 'Mark all read' }).classList.contains('btn-primary'),
    ).toBe(true);
    await confirm();
    expect(bulkRequests()).toEqual([
      [
        '/istota/api/briefings/mark-as-read',
        {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scope: 'all' }),
        },
      ],
    ]);
  });

  it('places the control before a registered Save and the settings cog', async () => {
    __history.reset('/istota/briefings/');
    render(Harness, {
      layout: Layout,
      component: HeaderSavePage,
      user: { username: 'alice' } as User,
    });
    await screen.findByRole('button', { name: 'Save changes' });
    const tools = screen
      .getByRole('button', { name: allLabel })
      .closest('.header-tools') as HTMLElement;
    expect(
      within(tools)
        .getAllByRole('button')
        .map((el) => el.title || el.textContent?.trim()),
    ).toEqual(['New briefing', allLabel, 'Save changes', 'Briefing settings']);
  });

  it('allows a no-op on an empty archive without fetching counts', async () => {
    archive = [];
    bulkResponse = async () => response({ status: 'ok', updated: 0 });
    __history.reset('/istota/briefings/');
    render(Harness, { layout: Layout, component: Page, user: { username: 'alice' } as User });
    await screen.findByRole('heading', { name: 'No briefings yet' });
    await fireEvent.click(screen.getByRole('button', { name: allLabel }));
    await confirm();
    expect(bulkRequests()).toHaveLength(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByText("Couldn't mark these as read.")).toBeNull();
    expect(get(selectedBriefingId)).toBeNull();
  });

  it('captures the exact name and renders it as text when the filter changes', async () => {
    await openReader(false, named);
    await fireEvent.click(screen.getByRole('button', { name: nameLabel }));
    expect(
      screen.getByText(
        `Are you sure you want to mark all unread editions of ${named} as read? This can't be undone.`,
      ),
    ).toBeTruthy();
    briefingFilterName.set('Evening');
    await settle();
    await confirm();
    expect(JSON.parse(bulkRequests()[0][1].body)).toEqual({
      scope: 'briefing',
      briefing_name: named,
    });
    expect(get(briefingFilterName)).toBe('Evening');
  });

  it('cancels without writing', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: allLabel }));
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await settle();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(bulkRequests()).toHaveLength(0);
  });

  it('closes the confirmation on settings navigation and keeps the button absent', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: allLabel }));
    __history.reset('/istota/briefings/settings/');
    await settle();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('button', { name: allLabel })).toBeNull();
    expect(bulkRequests()).toHaveLength(0);
    __history.reset('/istota/briefings/');
    await settle();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('closes the confirmation when the authenticated account changes', async () => {
    const view = await openReader();
    await fireEvent.click(screen.getByRole('button', { name: allLabel }));
    await view.rerender({ user: { username: 'bob', show_nav_unread_badges: true } as User });
    await settle();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(bulkRequests()).toHaveLength(0);
  });

  it('allows only one pending request and preserves the reader, pages, URL and scroll', async () => {
    let finish!: (value: Response) => void;
    bulkResponse = () =>
      new Promise((resolve) => {
        finish = resolve;
      });
    await openReader(false, named);
    await fireEvent.click(screen.getByRole('button', { name: 'Load older' }));
    await waitFor(() => expect(get(briefingArchiveCount)).toBe(40));
    await fireEvent.click(screen.getByRole('button', { name: /^Briefing 25 / }));
    await screen.findByText('Body of briefing 25');
    const url = window.location.href;
    const reader = screen.getByText('Body of briefing 25');
    const pane = reader.closest('.shell-main') as HTMLElement;
    pane.scrollTop = 250;
    const beforeCalls = fetchMock.mock.calls.length;
    const button = screen.getByRole('button', { name: nameLabel }) as HTMLButtonElement;
    await fireEvent.click(button);
    await confirm();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(button.disabled).toBe(true);
    await fireEvent.click(button);
    expect(bulkRequests()).toHaveLength(1);
    finish(response({ status: 'ok', updated: 42 }));
    await waitFor(() => expect(button.disabled).toBe(false));
    expect(fetchMock.mock.calls).toHaveLength(beforeCalls + 1);
    expect(get(selectedBriefingId)).toBe(25);
    expect(get(briefingFilterName)).toBe(named);
    expect(get(briefingArchiveCount)).toBe(40);
    expect(window.location.href).toBe(url);
    expect(screen.getByText('Body of briefing 25')).toBe(reader);
    expect(pane.scrollTop).toBe(250);
    await fireEvent.click(screen.getByRole('button', { name: 'Load older' }));
    await waitFor(() => expect(get(briefingArchiveCount)).toBe(42));
    expect(fetchMock.mock.calls.at(-1)?.[0]).toContain('offset=40');
  });

  it('shows a failure notice and retries only through another confirmation', async () => {
    bulkResponse = async () => response({ detail: 'Write unavailable' }, 500);
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: allLabel }));
    await confirm();
    await screen.findByText("Couldn't mark these as read.");
    expect(screen.getByText('Body of briefing 1')).toBeTruthy();
    expect(bulkRequests()).toHaveLength(1);
    bulkResponse = async () => response({ status: 'ok', updated: 0 });
    await fireEvent.click(screen.getByRole('button', { name: allLabel }));
    expect(bulkRequests()).toHaveLength(1);
    await confirm();
    expect(bulkRequests()).toHaveLength(2);
  });

  it('ignores a late failure after leaving the reader', async () => {
    let finish!: (value: Response) => void;
    bulkResponse = () =>
      new Promise((resolve) => {
        finish = resolve;
      });
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: allLabel }));
    await confirm();
    __history.reset('/istota/briefings/settings/');
    await settle();
    finish(response({}, 500));
    await settle();
    expect(screen.queryByText("Couldn't mark these as read.")).toBeNull();
  });

  it.each([
    ['all', '', allLabel],
    ['one name', named, nameLabel],
  ])('clears the archive rows unread marks after a %s bulk read', async (_scope, name, label) => {
    archive = archive.map((item) => ({ ...item, read_at: null }));
    await openReader(false, name);
    expect(screen.queryAllByText(', unread').length).toBeGreaterThan(0);
    await fireEvent.click(screen.getByRole('button', { name: label }));
    await confirm();
    await waitFor(() => expect(bulkRequests()).toHaveLength(1));
    await settle();
    // The fake archive still says unread, so only a local update can clear these.
    expect(screen.queryAllByText(', unread')).toHaveLength(0);
  });

  it('leaving the layout cancels an unconfirmed action', async () => {
    const view = await openReader();
    await fireEvent.click(screen.getByRole('button', { name: allLabel }));
    view.unmount();
    await settle();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(bulkRequests()).toHaveLength(0);
  });
});

it('refreshes authoritative counts after a named bulk read, retaining other names', async () => {
  archive = archive.map((item, index) => ({ ...item, read_at: index === 0 ? item.read_at : null }));
  const existing = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (url: string, init: RequestInit) => {
    if (url.endsWith('/nav/unread')) {
      return response({
        enabled: true,
        chat: 4,
        feeds: 108,
        briefings: archive.filter((item) => item.read_at === null).length,
      });
    }
    if (url.endsWith('/briefings/mark-as-read')) {
      const scope = JSON.parse(init.body as string);
      archive = archive.map((item) =>
        item.briefing_name === scope.briefing_name
          ? { ...item, read_at: '2026-01-03T12:00:00Z' }
          : item,
      );
      return response({ status: 'ok', updated: 41 });
    }
    return existing(url, init);
  });
  startNavUnreadPoll('alice');
  await openReader(true, named);
  await waitFor(() => expect(get(navUnreadCounts).briefings).toBe(44));
  await fireEvent.click(screen.getByRole('button', { name: nameLabel }));
  await confirm();
  await waitFor(() => expect(get(navUnreadCounts)).toEqual({ chat: 4, briefings: 3, feeds: 108 }), {
    timeout: 2000,
  });
  expect(bulkRequests()).toHaveLength(1);
});
