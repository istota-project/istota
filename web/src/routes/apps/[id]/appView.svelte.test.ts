import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import type { AppDetail, AppsList, AppSummary } from '$lib/apps/api';

const apps = vi.hoisted(() => ({
  getApps: vi.fn(),
  getApp: vi.fn(),
  uninstallApp: vi.fn(async () => {}),
  grantCapability: vi.fn(async () => ({ capability: '', scope: null })),
  callApp: vi.fn(async () => ({ ok: true as const, result: null })),
  reportAppError: vi.fn(async () => {}),
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
const identity = vi.hoisted(() => ({
  user: { username: 'alice', bot_name: 'Istota', features: { apps: true } },
  expireSession: vi.fn(),
  reload: vi.fn(),
}));
vi.mock('$lib/userContext', () => ({ getCurrentUser: () => identity }));
const connectivity = vi.hoisted(() => ({ online: null as unknown }));
vi.mock('$lib/stores/connectivity', async (importOriginal) => {
  const real = await importOriginal<typeof import('$lib/stores/connectivity')>();
  const { writable: makeWritable } = await import('svelte/store');
  connectivity.online = makeWritable(true);
  return { ...real, online: connectivity.online };
});

const Harness = (await import('./appViewInLayout.test.svelte')).default;
const online = () => connectivity.online as ReturnType<typeof writable<boolean>>;

/** The pane width every observed element reports, as the layout's ResizeObserver sees it. */
let paneWidth = 1200;
class FakeResizeObserver {
  constructor(private callback: ResizeObserverCallback) {}
  observe(target: Element) {
    const entry = { target, contentRect: { width: paneWidth } } as unknown as ResizeObserverEntry;
    this.callback([entry], this as unknown as ResizeObserver);
  }
  unobserve() {}
  disconnect() {}
}

function summary(overrides: Partial<AppSummary> = {}): AppSummary {
  return {
    app_id: 'board',
    name: 'Board',
    icon: 'kanban',
    description: '',
    origin: 'starter',
    current_version: 2,
    narrowest: 'phone',
    pending_id: null,
    ungranted: [],
    ...overrides,
  };
}

function detail(overrides: Partial<AppDetail> = {}): AppDetail {
  return {
    app_id: 'board',
    name: 'Board',
    icon: 'kanban',
    description: '',
    origin: 'starter',
    current_version: 2,
    installed_at: '2026-10-25T10:00:00Z',
    approved_at: '2026-10-25T10:00:00Z',
    manifest: {
      id: 'board',
      name: 'Board',
      icon: 'kanban',
      entry: 'index.html',
      description: '',
      capabilities: ['kv:own', 'files:own'],
      narrowest: 'phone',
      reason: '',
    },
    narrowest: 'phone',
    reason: '',
    warnings: [],
    pending_id: null,
    grants: [],
    ungranted: [],
    ...overrides,
  };
}

const MAP = { app_id: 'map', name: 'Memory map', narrowest: 'desktop' as const };

function list(): AppsList {
  return {
    apps: [summary(), summary({ ...MAP, icon: 'network', origin: 'starter' })],
    pending: [],
    drafts: [],
    starter: [],
    kit_built: true,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  paneWidth = 1200;
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  online().set(true);
  page.url = new URL('http://localhost/apps/board/');
  page.params = { id: 'board' };
  apps.getApps.mockResolvedValue(list());
  apps.getApp.mockImplementation(async (id: string) =>
    id === 'map' ? detail({ ...MAP, reason: 'A graph worth reading needs the width.' }) : detail(),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function openMap() {
  page.url = new URL('http://localhost/apps/map/');
  page.params = { id: 'map' };
}

async function frame(): Promise<HTMLIFrameElement> {
  return waitFor(() => {
    const el = document.querySelector('iframe');
    if (!el) throw new Error('no frame yet');
    return el;
  });
}

describe('the app view', () => {
  it('titles the app bar with the app and fills the pane with its frame', async () => {
    render(Harness);
    const el = await frame();
    expect(el.getAttribute('sandbox')).toBe('allow-scripts');
    expect(new URL(el.src).pathname).toBe('/api/apps/board/v/2/frame');
    expect(screen.getByRole('heading', { level: 1, name: 'Board' })).toBeTruthy();
  });

  it('offers Permissions, Versions, Activity, Reload and Uninstall in the kebab', async () => {
    render(Harness);
    await frame();
    await fireEvent.keyDown(screen.getByLabelText('Board actions'), { key: 'Enter' });
    const items = (await screen.findAllByRole('menuitem')).map((el) => el.textContent?.trim());
    expect(items).toEqual(['Permissions', 'Versions', 'Activity', 'Reload', 'Uninstall']);
    expect(screen.getByRole('menuitem', { name: 'Versions' })).toHaveAttribute(
      'href',
      '/apps/board/versions/',
    );
  });

  it('badges a waiting install and offers its review', async () => {
    apps.getApp.mockResolvedValue(detail({ pending_id: 'p-2' }));
    render(Harness);
    await frame();
    expect(screen.getByText('Update waiting')).toBeTruthy();
    await fireEvent.keyDown(screen.getByLabelText('Board actions'), { key: 'Enter' });
    expect(await screen.findByRole('menuitem', { name: 'Review update' })).toHaveAttribute(
      'href',
      '/apps/board/install/p-2/',
    );
  });

  it('shows an app made for a wider screen as an empty state, with Open anyway', async () => {
    paneWidth = 700;
    openMap();
    render(Harness);
    expect(await screen.findByText('Made for a wider screen')).toBeTruthy();
    expect(screen.getByText(/A graph worth reading needs the width/)).toBeTruthy();
    expect(document.querySelector('iframe')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Open anyway' }));
    expect((await frame()).getAttribute('sandbox')).toBe('allow-scripts');
    // Opened anyway, it says it may not fit rather than pretending it does.
    expect(screen.getByText('This app may not fit')).toBeTruthy();
  });

  it('gives the shipped Memory map its empty state on a phone, with its own reason', async () => {
    const shipped = readFileSync(
      join(process.cwd(), 'src', 'apps', 'starter', 'memory-map', 'manifest.toml'),
      'utf8',
    );
    const value = (key: string) => new RegExp(`^${key} = "(.*)"$`, 'm').exec(shipped)?.[1] ?? '';
    const narrowest = value('narrowest') as AppDetail['narrowest'];
    expect(narrowest).toBe('desktop');
    const memoryMap = { app_id: 'memory-map', name: value('name'), narrowest };
    apps.getApps.mockResolvedValue({ ...list(), apps: [summary(), summary(memoryMap)] });
    apps.getApp.mockResolvedValue(detail({ ...memoryMap, reason: value('reason') }));
    paneWidth = 375;
    page.url = new URL('http://localhost/apps/memory-map/');
    page.params = { id: 'memory-map' };
    render(Harness);
    expect(await screen.findByText('Made for a wider screen')).toBeTruthy();
    const message = screen.getByText(/Memory map is made for a desktop-width screen\./);
    expect(message.textContent).toContain(value('reason'));
    expect(document.querySelector('iframe')).toBeNull();
  });

  it('opens a desktop app straight away in a pane wide enough', async () => {
    paneWidth = 1100;
    openMap();
    render(Harness);
    await frame();
    expect(screen.queryByText('Made for a wider screen')).toBeNull();
  });

  it('badges an app the pane is too narrow for in the sidebar', async () => {
    paneWidth = 700;
    render(Harness);
    const sidebar = await waitFor(() => screen.getByRole('complementary'));
    const map = await within(sidebar).findByRole('link', { name: /Memory map/ });
    expect(within(map).getByText('Wider screen')).toBeTruthy();
    const board = within(sidebar).getByRole('link', { name: /Board/ });
    expect(within(board).queryByText('Wider screen')).toBeNull();
  });

  it('does not load the frame offline', async () => {
    online().set(false);
    render(Harness);
    expect(await screen.findByText('You are offline')).toBeTruthy();
    expect(document.querySelector('iframe')).toBeNull();
    online().set(true);
    await frame();
  });

  it('closes a frame that navigates itself and offers Reload', async () => {
    render(Harness);
    const el = await frame();
    el.dispatchEvent(new Event('load'));
    el.dispatchEvent(new Event('load'));
    expect(await screen.findByText('This app tried to leave its page')).toBeTruthy();
    expect(document.querySelector('iframe')).toBeNull();
    expect(apps.reportAppError).toHaveBeenCalledWith(
      'board',
      2,
      expect.objectContaining({ kind: 'navigated' }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    await frame();
    expect(apps.getApp).toHaveBeenCalledTimes(2);
  });

  it('uninstalls with the data when the box is checked, then goes back to All apps', async () => {
    render(Harness);
    await frame();
    await fireEvent.keyDown(screen.getByLabelText('Board actions'), { key: 'Enter' });
    const uninstall = await screen.findByRole('menuitem', { name: 'Uninstall' });
    uninstall.focus();
    await fireEvent.click(uninstall);
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByLabelText('Also delete its data'));
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Uninstall' }));
    await waitFor(() => expect(apps.uninstallApp).toHaveBeenCalledWith('board', true));
    await waitFor(() => expect(navigation.goto).toHaveBeenCalledWith('/apps/'));
  });

  it('says so when the app is not installed', async () => {
    const { AppsApiError } = await import('$lib/apps/api');
    apps.getApp.mockRejectedValue(new AppsApiError(404, 'app not found'));
    render(Harness);
    expect(await screen.findByText('This app is not installed.')).toBeTruthy();
  });

  describe('the first open of a starter app that asks for more', () => {
    const READ_NOTES = {
      capability: 'files:read:notes',
      scope: 'notes',
      description: 'Read your files in notes',
      sensitive: false,
    };

    function recall(overrides: Partial<AppDetail> = {}): AppDetail {
      return detail({ app_id: 'recall', name: 'Recall', ungranted: [READ_NOTES], ...overrides });
    }

    beforeEach(() => {
      page.url = new URL('http://localhost/apps/recall/');
      page.params = { id: 'recall' };
      apps.getApp.mockResolvedValue(recall());
    });

    it('asks before the frame loads, marked as shipped with Istota', async () => {
      render(Harness);
      expect(await screen.findByRole('heading', { name: 'Recall asks for more' })).toBeTruthy();
      expect(screen.getByText('Shipped with Istota')).toBeTruthy();
      expect(screen.getByText('Read your files in notes')).toBeTruthy();
      expect((screen.getByLabelText('Folder') as HTMLInputElement).value).toBe('notes');
      expect(document.querySelector('iframe')).toBeNull();
    });

    it('opens with only what is granted on Not now', async () => {
      render(Harness);
      await fireEvent.click(await screen.findByRole('button', { name: 'Not now' }));
      await frame();
      expect(apps.grantCapability).not.toHaveBeenCalled();
    });

    it('grants with the folder the user chose, then opens', async () => {
      render(Harness);
      const folder = await screen.findByLabelText('Folder');
      await fireEvent.input(folder, { target: { value: ' notes/aviation ' } });
      apps.getApp.mockResolvedValue(
        recall({ ungranted: [], grants: [{ ...READ_NOTES, scope: 'notes/aviation' }] }),
      );
      await fireEvent.click(screen.getByRole('button', { name: 'Allow' }));
      await waitFor(() =>
        expect(apps.grantCapability).toHaveBeenCalledWith(
          'recall',
          'files:read:notes',
          'notes/aviation',
        ),
      );
      await frame();
    });

    it('shows a refused folder under its field and stays', async () => {
      const { AppsApiError } = await import('$lib/apps/api');
      apps.grantCapability.mockRejectedValueOnce(
        new AppsApiError(422, 'files:read:memories: that folder cannot be granted.'),
      );
      render(Harness);
      await fireEvent.click(await screen.findByRole('button', { name: 'Allow' }));
      expect(await screen.findByText(/that folder cannot be granted/)).toBeTruthy();
      expect(document.querySelector('iframe')).toBeNull();
      expect(apps.getApp).toHaveBeenCalledTimes(1);
    });

    it('does not ask for an app installed through the approval screen', async () => {
      apps.getApp.mockResolvedValue(recall({ origin: 'draft' }));
      render(Harness);
      await frame();
      expect(screen.queryByText('Recall asks for more')).toBeNull();
    });
  });
});
