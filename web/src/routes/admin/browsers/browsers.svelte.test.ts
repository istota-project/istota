import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { __history } from '../../../../vitest-stubs/app-navigation';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
vi.mock('@novnc/novnc', () => ({ default: class {} }));
await fillApiDouble(api, { getAdminBrowsers: vi.fn() });
import Page from './+page.svelte';

const LIST = '/istota/admin/browsers/';
const currentUrl = () => __history.entries[__history.index].url;
const live = (...users: string[]) => ({
  status: 'ok',
  console_configured: false,
  instances: users.map((user, slot) => ({ user, slot, idle_seconds: 0, url: '' })),
});

function resetTo(url: string) {
  __history.reset(url);
  page.url = new URL(url, 'http://localhost');
}

beforeEach(() => {
  resetTo(LIST);
  // The step-up dialog asks for its request on mount; leave it waiting.
  api.startStepUp.mockReturnValue(new Promise(() => {}));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

it('lists live browsers with a View button and no external viewer', async () => {
  api.getAdminBrowsers.mockResolvedValue({
    status: 'ok',
    console_configured: true,
    instances: [
      { user: 'alice', slot: 2, idle_seconds: 125, url: 'https://console.example.com/vnc.html' },
      { user: 'bob', slot: 0, idle_seconds: 3, url: '' },
    ],
  });
  render(Page);
  expect(screen.getByText('Loading browsers…')).toBeTruthy();
  expect(await screen.findByRole('button', { name: "View alice's browser" })).toBeTruthy();
  expect(screen.getByRole('button', { name: "View bob's browser" })).toBeTruthy();
  expect(screen.getByText('2m 5s')).toBeTruthy();
  expect(screen.queryByRole('link', { name: 'Open VNC' })).toBeNull();
  expect(screen.queryByText(/No external VNC URL/)).toBeNull();
});

it('says nothing about a missing viewer URL', async () => {
  api.getAdminBrowsers.mockResolvedValue(live('alice'));
  render(Page);
  expect(await screen.findByRole('button', { name: "View alice's browser" })).toBeTruthy();
  expect(screen.queryByText(/No external VNC URL/)).toBeNull();
  expect(screen.queryByText('Unavailable')).toBeNull();
});

it.each([
  ['disabled', 'Browser service is disabled.'],
  ['unavailable', 'Browser service is unavailable.'],
  ['ok', 'No live browsers.'],
])('renders the %s state', async (status, message) => {
  api.getAdminBrowsers.mockResolvedValue({ status, console_configured: true, instances: [] });
  render(Page);
  expect(await screen.findByText(message)).toBeTruthy();
});

it('says when the scheduler read the pool', async () => {
  const asOf = '2026-10-11T12:00:00Z';
  api.getAdminBrowsers.mockResolvedValue({
    status: 'ok',
    console_configured: true,
    instances: [],
    as_of: asOf,
  });
  render(Page);
  const time = await screen.findByText(new Date(asOf).toLocaleTimeString());
  expect(time.tagName).toBe('TIME');
  expect(time.parentElement?.textContent?.trim()).toMatch(/^As of /);
});

it('opens the console in the page from View and puts the user in the URL', async () => {
  api.getAdminBrowsers.mockResolvedValue(live('alice'));
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: "View alice's browser" }));
  expect(new URL(currentUrl(), 'http://localhost').searchParams.get('user')).toBe('alice');
  expect(await screen.findByRole('heading', { name: 'alice' })).toBeTruthy();
  expect(screen.getByRole('link', { name: /Browsers/ }).getAttribute('href')).toBe(
    '/admin/browsers',
  );
  expect(screen.queryByRole('table')).toBeNull();
  expect(await screen.findByRole('dialog', { name: 'Confirm sensitive action' })).toBeTruthy();
});

it('stops polling while a console is open and starts again on Back', async () => {
  vi.useFakeTimers();
  api.getAdminBrowsers.mockResolvedValue(live('alice'));
  render(Page);
  await vi.advanceTimersByTimeAsync(0);
  await fireEvent.click(screen.getByRole('button', { name: "View alice's browser" }));
  await tick();
  const calls = api.getAdminBrowsers.mock.calls.length;
  await vi.advanceTimersByTimeAsync(30_000);
  expect(api.getAdminBrowsers).toHaveBeenCalledTimes(calls);

  __history.back();
  await vi.advanceTimersByTimeAsync(0);
  expect(screen.queryByRole('heading', { name: 'alice' })).toBeNull();
  expect(screen.getByRole('button', { name: "View alice's browser" })).toBeTruthy();
  expect(api.getAdminBrowsers).toHaveBeenCalledTimes(calls + 1);
  await vi.advanceTimersByTimeAsync(10_000);
  expect(api.getAdminBrowsers).toHaveBeenCalledTimes(calls + 2);
});

it('opens a deep-linked console without waiting on the list', async () => {
  resetTo(`${LIST}?user=alice`);
  api.getAdminBrowsers.mockResolvedValue({
    status: 'unavailable',
    console_configured: false,
    instances: [],
  });
  render(Page);
  expect(await screen.findByRole('heading', { name: 'alice' })).toBeTruthy();
  expect(screen.queryByText('Browser service is unavailable.')).toBeNull();
  expect(api.getAdminBrowsers).not.toHaveBeenCalled();
});

it('returns to the list when the step-up is cancelled', async () => {
  api.startStepUp.mockResolvedValue({
    request_id: 'req-1',
    method: 'password',
    methods: ['password'],
  });
  api.getAdminBrowsers.mockResolvedValue(live('alice'));
  resetTo(`${LIST}?user=alice`);
  render(Page);
  const dialog = await screen.findByRole('dialog', { name: 'Confirm sensitive action' });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  await waitFor(() => expect(screen.queryByRole('heading', { name: 'alice' })).toBeNull());
  expect(new URL(currentUrl(), 'http://localhost').searchParams.get('user')).toBeNull();
  expect(await screen.findByRole('button', { name: "View alice's browser" })).toBeTruthy();
});

it('refreshes discovery and stops polling when closed', async () => {
  vi.useFakeTimers();
  api.getAdminBrowsers.mockResolvedValue({ status: 'ok', console_configured: true, instances: [] });
  const view = render(Page);
  await vi.advanceTimersByTimeAsync(0);
  api.getAdminBrowsers.mockResolvedValue(live('bob'));
  await vi.advanceTimersByTimeAsync(10000);
  expect(screen.getByText('bob')).toBeTruthy();
  api.getAdminBrowsers.mockResolvedValue({
    status: 'unavailable',
    console_configured: true,
    instances: [],
  });
  await vi.advanceTimersByTimeAsync(10000);
  expect(screen.queryByText('bob')).toBeNull();
  expect(screen.getByText('Browser service is unavailable.')).toBeTruthy();
  view.unmount();
  const calls = api.getAdminBrowsers.mock.calls.length;
  await vi.advanceTimersByTimeAsync(20000);
  expect(api.getAdminBrowsers).toHaveBeenCalledTimes(calls);
});

it('reports a failed request without leaving live links on screen', async () => {
  api.getAdminBrowsers.mockRejectedValue(new Error('offline'));
  render(Page);
  await waitFor(() => expect(screen.getByText('Browser service is unavailable.')).toBeTruthy());
});
