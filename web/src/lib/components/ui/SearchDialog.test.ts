import { base } from '$app/paths';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { SearchGroup, SearchHit } from '$lib/api';
import * as navigation from '../../../../vitest-stubs/app-navigation';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import SearchDialog from './SearchDialog.svelte';
import { createSearch } from '$lib/stores/search';
import { viewer } from '$lib/fileViewer/store.svelte';
const hit = (id: string): SearchHit => ({
  id,
  kind: 'message',
  title: id,
  subtitle: 'You',
  snippet: '<img onerror> hello',
  highlights: [[14, 19]],
  date: null,
  badges: [],
  link: { type: 'route', path: '/chat/', params: { room: 'A', msg: id } },
});
const group = (source: string, results: SearchHit[], extra = {}): SearchGroup => ({
  source,
  label: source,
  results,
  has_more: false,
  relaxed: false,
  error: null,
  elapsed_ms: 0,
  ...extra,
});
let controller: ReturnType<typeof createSearch>;
beforeEach(() => {
  vi.clearAllMocks();
  controller = createSearch();
  vi.spyOn(navigation, 'goto');
});
afterEach(() => {
  cleanup();
  controller.close();
  viewer.close();
  vi.restoreAllMocks();
});
async function open(groups: SearchGroup[]) {
  api.search.mockResolvedValue({
    query: 'hello',
    groups,
    on_demand: [{ source: 'money', label: 'Transactions' }],
  });
  render(SearchDialog, { open: true, controller });
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'hello' } });
  await waitFor(() => expect(screen.getAllByRole('option').length).toBeGreaterThan(0));
}
it('renders ordered groups, partial matches, source errors and escaped highlights', async () => {
  await open([
    group('chats', [hit('1')]),
    group('empty', []),
    group('facts', [hit('2')], { relaxed: true }),
    group('health', [], { error: 'timeout' }),
  ]);
  expect(screen.getAllByRole('option').map((el) => el.textContent)).toEqual([
    expect.stringContaining('1'),
    expect.stringContaining('2'),
  ]);
  expect(screen.queryByRole('group', { name: 'empty' })).toBeNull();
  expect(screen.getByText('No exact matches. Showing partial matches.')).toBeInTheDocument();
  expect(screen.getByText("Couldn't search health.")).toBeInTheDocument();
  expect(document.querySelector('mark')?.textContent).toBe('hello');
  expect(document.querySelector('[role="listbox"] img')).toBeNull();
  expect(screen.getAllByText('<img onerror>', { exact: false }).length).toBeGreaterThan(0);
});
it('wraps across groups and opens the active route on Enter', async () => {
  await open([group('chats', [hit('1')]), group('facts', [hit('2')])]);
  const input = screen.getByRole('combobox');
  await fireEvent.keyDown(input, { key: 'ArrowUp' });
  expect(screen.getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true');
  await fireEvent.keyDown(input, { key: 'ArrowDown' });
  expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true');
  await fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(navigation.goto).toHaveBeenCalledWith(`${base}/chat/?room=A&msg=1`));
  expect(screen.queryByRole('dialog')).toBeNull();
});
it('filters, appends a second page, and explicitly requests transactions', async () => {
  await open([group('chats', [hit('1')])]);
  api.search.mockResolvedValueOnce({
    query: 'hello',
    groups: [group('chats', [hit('2')], { has_more: true })],
  });
  await fireEvent.click(screen.getByRole('button', { name: 'chats' }));
  await waitFor(() =>
    expect(api.search).toHaveBeenLastCalledWith(
      'hello',
      expect.objectContaining({ sources: ['chats'], limit: 20, offset: 0 }),
    ),
  );
  await screen.findByRole('button', { name: 'Show more' });
  api.search.mockResolvedValueOnce({ query: 'hello', groups: [group('chats', [hit('3')])] });
  await fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
  await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2));
  expect(api.search).toHaveBeenLastCalledWith(
    'hello',
    expect.objectContaining({ sources: ['chats'], offset: 20 }),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'All' }));
  await screen.findByRole('button', { name: 'Search Transactions' });
  await fireEvent.click(screen.getByRole('button', { name: 'Search Transactions' }));
  await waitFor(() =>
    expect(api.search).toHaveBeenLastCalledWith(
      'hello',
      expect.objectContaining({ sources: ['money'], limit: 20 }),
    ),
  );
});
it('opens files through the viewer and skips results with refused routes', async () => {
  await open([
    group('memory', [
      { ...hit('blocked'), link: { type: 'route', path: '/unknown/', params: {} } },
      { ...hit('file'), link: { type: 'file', path: '/Users/alice/memories/note.md' } },
    ]),
  ]);
  expect(screen.getAllByRole('option')[0]).toBeDisabled();
  await fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' });
  await waitFor(() =>
    expect(viewer.state).toEqual({ mode: 'file', path: '/Users/alice/memories/note.md' }),
  );
  expect(navigation.goto).not.toHaveBeenCalled();
});
it('keeps failures in the dialog and retries on the next query', async () => {
  api.search.mockRejectedValueOnce(new Error('offline'));
  render(SearchDialog, { open: true, controller });
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'hello' } });
  expect(await screen.findByText('Search is unavailable right now.')).toBeInTheDocument();
  api.search.mockResolvedValueOnce({ query: 'again' });
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'again' } });
  await waitFor(() => expect(screen.queryByText('Search is unavailable right now.')).toBeNull());
  expect(await screen.findByText('No matches.')).toBeInTheDocument();
});
