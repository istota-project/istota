import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { __history } from '../../../../vitest-stubs/app-navigation';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import { getAdminLogSources, getAdminLogPage, type AdminLogSource } from '$lib/api';
import Page from './+page.svelte';

const sources: AdminLogSource[] = ['app', 'tasks'].map((id) => ({
  id,
  label: id === 'app' ? 'Application' : 'Tasks',
  available: true,
  kind: id === 'app' ? 'file' : 'db',
  description: '',
  detail: '',
  time_basis: 'utc',
  path: null,
  bytes: 0,
  files: 0,
}));
const currentUrl = () => __history.entries[__history.index].url;

beforeEach(() => {
  vi.clearAllMocks();
  __history.reset('/istota/admin/logs/');
  vi.mocked(getAdminLogSources).mockResolvedValue({ sources });
  vi.mocked(getAdminLogPage).mockResolvedValue({
    records: [],
    next_before: null,
    tail_cursor: null,
    truncated: false,
  });
});
afterEach(cleanup);

describe('log source history', () => {
  it('pushes source picks and fetches the prior source on Back', async () => {
    render(Page);
    await waitFor(() => expect(getAdminLogPage).toHaveBeenCalledWith('app', expect.anything()));
    const trigger = screen.getByRole('button', { name: 'Log source' });
    await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(trigger);
    const option = await screen.findByRole('option', { name: 'Tasks' });
    await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(option);
    await waitFor(() => expect(getAdminLogPage).toHaveBeenCalledWith('tasks', expect.anything()));
    expect(currentUrl()).toBe('/istota/admin/logs/?source=tasks');
    expect(__history.entries).toHaveLength(2);
    vi.mocked(getAdminLogPage).mockClear();
    __history.back();
    await waitFor(() => expect(getAdminLogPage).toHaveBeenCalledWith('app', expect.anything()));
    expect(__history.entries).toHaveLength(2);
  });

  it('keeps the loaded source URL until sources arrive, then fetches it', async () => {
    __history.reset('/istota/admin/logs/?source=tasks');
    let finish!: (value: { sources: AdminLogSource[] }) => void;
    vi.mocked(getAdminLogSources).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    render(Page);
    await tick();
    expect(currentUrl()).toBe('/istota/admin/logs/?source=tasks');
    expect(getAdminLogPage).not.toHaveBeenCalled();
    finish({ sources });
    await waitFor(() => expect(getAdminLogPage).toHaveBeenCalledWith('tasks', expect.anything()));
    expect(getAdminLogPage).toHaveBeenCalledTimes(1);
    expect(__history.entries).toHaveLength(1);
  });

  it('replaces an unknown source with the app default', async () => {
    __history.reset('/istota/admin/logs/?source=missing');
    render(Page);
    await waitFor(() => expect(currentUrl()).toBe('/istota/admin/logs/?source=app'));
    expect(getAdminLogPage).toHaveBeenCalledWith('app', expect.anything());
    expect(__history.entries).toHaveLength(1);
  });
});
