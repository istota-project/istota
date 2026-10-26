/**
 * Activity: an app's last calls, every version, and its current version's
 * error reports. An app's own text is rendered as text.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/svelte';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import { AppsApiError, type AppActivity, type AppsList } from '$lib/apps/api';

const apps = vi.hoisted(() => ({
  getApps: vi.fn(),
  getActivity: vi.fn(),
}));
vi.mock('$lib/apps/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/apps/api')>()),
  ...apps,
}));
const identity = vi.hoisted(() => ({
  user: { username: 'alice', bot_name: 'Istota', features: { apps: true } },
  expireSession: vi.fn(),
  reload: vi.fn(),
}));
vi.mock('$lib/userContext', () => ({ getCurrentUser: () => identity }));

const Harness = (await import('./activityInLayout.test.svelte')).default;

const LIST: AppsList = {
  apps: [
    {
      app_id: 'tides',
      name: 'Tides',
      icon: 'chart-line',
      description: '',
      origin: 'draft',
      current_version: 2,
      narrowest: 'phone',
      pending_id: null,
      ungranted: [],
    },
  ],
  pending: [],
  drafts: [],
  starter: [],
  kit_built: true,
};

function activity(): AppActivity {
  return {
    app_id: 'tides',
    current_version: 2,
    calls: [
      {
        id: 3,
        version: 2,
        capability: 'calendar:read',
        op: 'list',
        status: 'ok',
        at: '2026-10-24T10:02:00Z',
      },
      {
        id: 2,
        version: 2,
        capability: 'kv:read:journal',
        op: 'get',
        status: 'not_granted',
        at: '2026-10-24T10:01:00Z',
      },
      {
        id: 1,
        version: 1,
        capability: 'kv:own',
        op: 'set',
        status: 'ok',
        at: '2026-10-20T10:00:00Z',
      },
    ],
    errors: [
      {
        id: 9,
        version: 2,
        kind: 'navigated',
        message: '<img src=x onerror=alert(1)> left the frame',
        stack: 'at boot (app.js:1)',
        at: '2026-10-24T10:03:00Z',
      },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  page.url = new URL('http://localhost/apps/activity/?id=tides');
  apps.getApps.mockResolvedValue(LIST);
  apps.getActivity.mockResolvedValue(activity());
});
afterEach(() => cleanup());

function card(name: string): HTMLElement {
  return screen.getByRole('heading', { name }).closest('section') as HTMLElement;
}

describe('Activity', () => {
  it('lists the calls newest first, with what each asked for and how it was answered', async () => {
    render(Harness);
    await screen.findByRole('heading', { name: 'Calls' });
    expect(apps.getActivity).toHaveBeenCalledWith('tides');
    const rows = within(card('Calls')).getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getAllByRole('cell')[1].textContent?.trim())).toEqual([
      'calendar:read',
      'kv:read:journal',
      'kv:own',
    ]);
    expect(within(rows[1]).getByText('Not granted')).toBeTruthy();
    expect(within(rows[0]).getByText('Answered')).toBeTruthy();
    expect(within(rows[2]).getByText('1')).toBeTruthy();
    expect(screen.getByRole('link', { name: /Back to Tides/ })).toHaveAttribute(
      'href',
      '/apps/open/?id=tides',
    );
  });

  it("shows the current version's errors with the app's text as text", async () => {
    const { container } = render(Harness);
    const errors = within(
      await screen
        .findByRole('heading', { name: 'Errors in version 2' })
        .then(() => card('Errors in version 2')),
    );
    expect(errors.getByText('<img src=x onerror=alert(1)> left the frame')).toBeTruthy();
    expect(errors.getByText('Navigated away')).toBeTruthy();
    expect(container.querySelector('img[src="x"]')).toBeNull();
  });

  it('says so when there is nothing to show', async () => {
    apps.getActivity.mockResolvedValue({ ...activity(), calls: [], errors: [] });
    render(Harness);
    await screen.findByRole('heading', { name: 'Calls' });
    expect(within(card('Calls')).getByText('This app has made no calls.')).toBeTruthy();
    expect(
      within(card('Errors in version 2')).getByText('Version 2 has reported no errors.'),
    ).toBeTruthy();
  });

  it('says the app is not installed on a 404', async () => {
    apps.getActivity.mockRejectedValue(new AppsApiError(404, 'app not found'));
    render(Harness);
    expect(await screen.findByText('This app is not installed.')).toBeTruthy();
  });
});
