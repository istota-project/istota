import { base } from '$app/paths';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { SearchGroup, SearchHit, SearchResponse } from '$lib/api';
import moneyResponses from '$lib/test/fixtures/money-search.json';
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
  render(SearchDialog, { open: true, controller, features: { money: true } });
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'hello' } });
  await waitFor(() => expect(screen.getAllByRole('option').length).toBeGreaterThan(0));
}
async function chooseSource(label: string) {
  const trigger = screen.getByRole('button', { name: 'Search sources' });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const option = await screen.findByRole('option', { name: label });
  await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(option);
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
  await chooseSource('Chats');
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
  await chooseSource('All');
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
  render(SearchDialog, { open: true, controller, features: { money: true } });
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'hello' } });
  expect(await screen.findByText('Search is unavailable right now.')).toBeInTheDocument();
  api.search.mockResolvedValueOnce({ query: 'again' });
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'again' } });
  await waitFor(() => expect(screen.queryByText('Search is unavailable right now.')).toBeNull());
  expect(await screen.findByText('No matches.')).toBeInTheDocument();
});

it('opens an on-demand transaction from the authenticated ledger response', async () => {
  api.search.mockImplementation(async (_q, options) =>
    options?.sources?.includes('money')
      ? (moneyResponses.money as SearchResponse)
      : (moneyResponses.all as SearchResponse),
  );
  render(SearchDialog, { open: true, controller, features: { money: true } });
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'Coffee' } });
  await fireEvent.click(await screen.findByRole('button', { name: 'Search Transactions' }));
  await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3));
  expect(api.search).toHaveBeenLastCalledWith(
    'Coffee',
    expect.objectContaining({ sources: ['money'], limit: 20 }),
  );
  expect(screen.getAllByRole('option')[0]).toHaveTextContent('Acme');
  expect(screen.getAllByRole('option')[0]).toHaveTextContent('Expenses:Food:Coffee');
  expect(screen.getAllByRole('option')[0].querySelector('mark')).toHaveTextContent('Coffee');
  await fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' });
  await waitFor(() =>
    expect(navigation.goto).toHaveBeenCalledWith(
      `${base}/money/transactions/?account=Expenses%3AFood%3ACoffee&year=2024`,
    ),
  );
});

it('places the source dropdown beside the query and the close icon in the header', async () => {
  render(SearchDialog, { open: true, controller, features: { money: true } });
  const input = screen.getByRole('combobox', { name: 'Search everything' });
  const source = screen.getByRole('button', { name: 'Search sources' });
  expect(input.parentElement).toContainElement(source);
  expect(source).toHaveTextContent('All');
  const title = screen.getByRole('heading', { name: 'Search' });
  const close = within(title.parentElement!).getByRole('button', { name: 'Close' });
  expect(close.querySelector('svg')).not.toBeNull();
  expect(input.parentElement).not.toContainElement(close);
  await waitFor(() => expect(input).toHaveFocus());
  await fireEvent.click(close);
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('lists sources before typing and searches only the source chosen first', async () => {
  api.search.mockResolvedValue({ query: 'hello', groups: [] });
  render(SearchDialog, { open: true, controller, features: { feeds: true, money: true } });
  const trigger = screen.getByRole('button', { name: 'Search sources' });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  expect(screen.getAllByRole('option').map((option) => option.textContent?.trim())).toEqual([
    'All',
    'Chats',
    'Rooms',
    'Memory',
    'Facts',
    'Feeds',
    'Transactions',
  ]);
  expect(api.search).not.toHaveBeenCalled();
  const transactions = screen.getByRole('option', { name: 'Transactions' });
  await fireEvent.pointerUp(transactions, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(transactions);
  expect(api.search).not.toHaveBeenCalled();
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'hello' } });
  await waitFor(() =>
    expect(api.search).toHaveBeenCalledExactlyOnceWith(
      'hello',
      expect.objectContaining({ sources: ['money'], limit: 20 }),
    ),
  );
});
