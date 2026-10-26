/**
 * The approval screen: what the user consents to before a pending install
 * becomes a version. It is a security surface, so these hold what it shows
 * (every capability, the new ones marked, the health warning, the display
 * range, the size, the warnings and the source) and the two shapes of the
 * approval: one button at the top when nothing new is asked, the button only
 * after the capability list otherwise, echoing back the list it showed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import type { AppsList, PendingCapability, PendingInstall } from '$lib/apps/api';

const apps = vi.hoisted(() => ({
  getApps: vi.fn(),
  getPending: vi.fn(),
  approvePending: vi.fn(),
  deletePending: vi.fn(async () => {}),
}));
vi.mock('$lib/apps/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/apps/api')>()),
  ...apps,
}));
const navigation = vi.hoisted(() => ({ goto: vi.fn(async () => {}) }));
vi.mock('$app/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$app/navigation')>()),
  goto: navigation.goto,
}));
const notices = vi.hoisted(() => ({
  notifyError: vi.fn(),
  notifyWarning: vi.fn(),
  notifySuccess: vi.fn(),
}));
vi.mock('@minot/ui/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@minot/ui/stores/notices')>()),
  ...notices,
}));
const identity = vi.hoisted(() => ({
  user: { username: 'alice', bot_name: 'Istota', features: { apps: true } },
  expireSession: vi.fn(),
  reload: vi.fn(),
}));
vi.mock('$lib/userContext', () => ({ getCurrentUser: () => identity }));

const { AppsApiError } = await import('$lib/apps/api');
const Harness = (await import('./installInLayout.test.svelte')).default;

function cap(capability: string, overrides: Partial<PendingCapability> = {}): PendingCapability {
  const always = capability === 'kv:own' || capability === 'files:own';
  return {
    capability,
    scope: null,
    description: `Plain words for ${capability}`,
    sensitive: false,
    new: false,
    always_granted: always,
    ...overrides,
  };
}

function pending(overrides: Partial<PendingInstall> = {}): PendingInstall {
  return {
    pending_id: 'p-1',
    app_id: 'tides',
    origin: 'draft',
    created_at: '2026-10-25T10:00:00Z',
    manifest: {
      id: 'tides',
      name: 'Tides',
      icon: 'chart-line',
      entry: 'index.html',
      description: '',
      capabilities: ['kv:own', 'files:own'],
      narrowest: 'phone',
      reason: '',
    },
    narrowest: 'phone',
    reason: '',
    current_version: 2,
    capabilities: [cap('kv:own'), cap('files:own')],
    new_capabilities: [],
    one_button: true,
    size: 2048,
    warnings: [],
    diff: [
      {
        path: 'index.html',
        status: 'changed',
        diff: '--- a/index.html\n+++ b/index.html\n@@ -1 +1 @@\n-<p>old</p>\n+<p>new</p>\n',
      },
      { path: 'app.css', status: 'unchanged', diff: '' },
    ],
    ...overrides,
  };
}

/** A version asking for something its current version was not granted. */
function widening(): PendingInstall {
  return pending({
    capabilities: [
      cap('kv:own'),
      cap('files:own'),
      cap('calendar:read', { new: true, description: 'Read events from all your calendars' }),
      cap('skill:health.trend', {
        new: true,
        sensitive: true,
        scope: 'health.trend',
        description: 'Read how one of your health measures has changed',
      }),
    ],
    new_capabilities: ['calendar:read', 'skill:health.trend'],
    one_button: false,
  });
}

const LIST: AppsList = { apps: [], pending: [], drafts: [], starter: [], kit_built: true };

beforeEach(() => {
  vi.clearAllMocks();
  page.url = new URL('http://localhost/apps/install/?id=tides&pending=p-1');
  apps.getApps.mockResolvedValue(LIST);
  apps.getPending.mockResolvedValue(pending());
  apps.approvePending.mockResolvedValue({ app_id: 'tides', version: 3 });
});
afterEach(() => cleanup());

async function permissions(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: 'Permissions' });
  return heading.closest('section') as HTMLElement;
}

function before(a: Node, b: Node): boolean {
  return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

describe('the approval screen', () => {
  it('loads the pending install it was opened for', async () => {
    render(Harness);
    await permissions();
    expect(apps.getPending).toHaveBeenCalledWith('tides', 'p-1');
  });

  it('lists every capability with its plain words, the always-allowed ones marked', async () => {
    render(Harness);
    const card = within(await permissions());
    expect(card.getByText('Plain words for kv:own')).toBeTruthy();
    expect(card.getByText('kv:own')).toBeTruthy();
    expect(card.getAllByText('Always allowed')).toHaveLength(2);
    expect(card.queryByText('New')).toBeNull();
  });

  it('marks the capabilities that are new against the current version', async () => {
    apps.getPending.mockResolvedValue(widening());
    render(Harness);
    const card = await permissions();
    const rows = within(card).getAllByRole('listitem');
    const calendar = rows.find((row) => row.textContent?.includes('calendar:read'))!;
    expect(within(calendar).getByText('New')).toBeTruthy();
    expect(within(calendar).getByText('Read events from all your calendars')).toBeTruthy();
    const own = rows.find((row) => row.textContent?.includes('kv:own'))!;
    expect(within(own).queryByText('New')).toBeNull();
  });

  it('warns when the app can read health records', async () => {
    apps.getPending.mockResolvedValue(widening());
    render(Harness);
    await permissions();
    expect(screen.getByText('This app can read your health records')).toBeTruthy();
  });

  it('does not warn about health for an app that cannot read it', async () => {
    render(Harness);
    await permissions();
    expect(screen.queryByText('This app can read your health records')).toBeNull();
  });

  it('shows the display range with its reason, the size and the warnings', async () => {
    apps.getPending.mockResolvedValue(
      pending({
        narrowest: 'desktop',
        reason: 'A graph worth reading needs the width.',
        size: 1536,
        warnings: ['index.html: alert( shows no dialog in the iOS app.'],
      }),
    );
    render(Harness);
    await permissions();
    expect(screen.getByText(/made for a desktop-width screen/i)).toBeTruthy();
    expect(screen.getByText('A graph worth reading needs the width.')).toBeTruthy();
    expect(screen.getByText('2 KB')).toBeTruthy();
    expect(screen.getByText('index.html: alert( shows no dialog in the iOS app.')).toBeTruthy();
  });

  it('says a phone app works on a phone, with no reason line', async () => {
    render(Harness);
    await permissions();
    expect(screen.getByText(/works on a phone/i)).toBeTruthy();
  });

  it('puts the source diff last, in a disclosure', async () => {
    render(Harness);
    const card = await permissions();
    const summary = screen.getByText(/Source changes/);
    const details = summary.closest('details') as HTMLDetailsElement;
    expect(details).toBeTruthy();
    expect(details.open).toBe(false);
    expect(before(card, details)).toBe(true);
    const code = within(details).getByText(/\+<p>new<\/p>/);
    expect(code.closest('pre')).toBeTruthy();
    expect(within(details).getByText('app.css')).toBeTruthy();
    expect(within(details).getByText('Unchanged')).toBeTruthy();
  });

  it('calls the source of a first install "Source", not changes', async () => {
    apps.getPending.mockResolvedValue(
      pending({
        current_version: null,
        diff: [{ path: 'index.html', status: 'added', diff: '+<p>hi</p>\n' }],
      }),
    );
    render(Harness);
    await permissions();
    expect(screen.queryByText(/Source changes/)).toBeNull();
    expect(screen.getByText(/^Source/)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Install Tides' })).toBeTruthy();
  });

  it('approves with one button at the top when nothing new is asked', async () => {
    render(Harness);
    const card = await permissions();
    const approve = screen.getByRole('button', { name: 'Update' });
    expect(before(approve, card)).toBe(true);
    expect(screen.getAllByRole('button', { name: /^(Update|Allow and update)$/ })).toHaveLength(1);
    await fireEvent.click(approve);
    await waitFor(() => expect(apps.approvePending).toHaveBeenCalledWith('tides', 'p-1', []));
    await waitFor(() => expect(navigation.goto).toHaveBeenCalledWith('/apps/open/?id=tides'));
    expect(apps.getApps).toHaveBeenCalledTimes(2);
  });

  it('offers approval only after the list when new capabilities are asked, and echoes them', async () => {
    apps.getPending.mockResolvedValue(widening());
    render(Harness);
    const card = await permissions();
    expect(screen.queryByRole('button', { name: 'Update' })).toBeNull();
    const approve = screen.getByRole('button', { name: 'Allow and update' });
    const lastRow = within(card).getAllByRole('listitem').at(-1)!;
    expect(before(lastRow, approve)).toBe(true);
    await fireEvent.click(approve);
    await waitFor(() =>
      expect(apps.approvePending).toHaveBeenCalledWith('tides', 'p-1', [
        'calendar:read',
        'skill:health.trend',
      ]),
    );
  });

  it('reloads the screen when the approval was made on a stale one', async () => {
    apps.getPending.mockResolvedValue(widening());
    apps.approvePending.mockRejectedValue(
      new AppsApiError(409, 'stale', [], {
        error: 'stale',
        message: 'The permissions changed.',
        new_capabilities: ['calendar:read'],
      }),
    );
    render(Harness);
    await permissions();
    await fireEvent.click(screen.getByRole('button', { name: 'Allow and update' }));
    await waitFor(() => expect(apps.getPending).toHaveBeenCalledTimes(2));
    expect(notices.notifyWarning).toHaveBeenCalled();
    expect(navigation.goto).not.toHaveBeenCalled();
  });

  it('says so when the install is no longer waiting', async () => {
    apps.getPending.mockRejectedValue(
      new AppsApiError(404, "That install is no longer waiting. Reopen it from the app's folder."),
    );
    render(Harness);
    expect(await screen.findByText(/no longer waiting/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Update|Install/ })).toBeNull();
  });

  it('discards the pending install and goes back to All apps', async () => {
    render(Harness);
    await permissions();
    await fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    await waitFor(() => expect(apps.deletePending).toHaveBeenCalledWith('tides', 'p-1'));
    await waitFor(() => expect(navigation.goto).toHaveBeenCalledWith('/apps/'));
    expect(apps.approvePending).not.toHaveBeenCalled();
  });
});
