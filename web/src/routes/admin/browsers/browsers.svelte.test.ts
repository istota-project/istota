import { afterEach, expect, it, vi } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, { getAdminBrowsers: vi.fn() });
import Page from './+page.svelte';
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

it('renders a direct viewer link and idle duration', async () => {
  api.getAdminBrowsers.mockResolvedValue({
    status: 'ok',
    console_configured: true,
    instances: [
      {
        user: 'alice',
        slot: 2,
        idle_seconds: 125,
        url: 'https://console.example.com/vnc.html?path=route',
      },
    ],
  });
  render(Page);
  expect(screen.getByText('Loading browsers…')).toBeTruthy();
  const link = await screen.findByRole('link', { name: 'Open VNC' });
  expect(link.getAttribute('href')).toBe('https://console.example.com/vnc.html?path=route');
  expect(link.getAttribute('target')).toBe('_blank');
  expect(screen.getByText('alice')).toBeTruthy();
  expect(screen.getByText('2m 5s')).toBeTruthy();
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

it('explains missing console configuration without a viewer link', async () => {
  api.getAdminBrowsers.mockResolvedValue({
    status: 'ok',
    console_configured: false,
    instances: [{ user: 'alice', slot: 0, idle_seconds: 0, url: '' }],
  });
  render(Page);
  expect(await screen.findByText(/No external VNC URL is configured/)).toBeTruthy();
  expect(screen.queryByRole('link', { name: 'Open VNC' })).toBeNull();
});

it('refreshes discovery and stops polling when closed', async () => {
  vi.useFakeTimers();
  api.getAdminBrowsers.mockResolvedValue({ status: 'ok', console_configured: true, instances: [] });
  const view = render(Page);
  await vi.advanceTimersByTimeAsync(0);
  api.getAdminBrowsers.mockResolvedValue({
    status: 'ok',
    console_configured: true,
    instances: [
      { user: 'bob', slot: 0, idle_seconds: 0, url: 'https://console.example.com/vnc.html' },
    ],
  });
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
