import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import SharingCard from './SharingCard.svelte';
import {
  AuthError,
  addSharedFeed,
  removeSharedFeed,
  type SharedFeed,
  type SharingSettings,
} from '$lib/api';

function feed(overrides: Partial<SharedFeed> = {}): SharedFeed {
  return {
    id: 12,
    host: 'cal.example.org',
    name: 'Work',
    state: 'active',
    last_reason: null,
    last_ok_at: '2026-10-25T10:00:00Z',
    created_at: '2026-10-20T10:00:00Z',
    ...overrides,
  };
}

function settings(overrides: Partial<SharingSettings> = {}): SharingSettings {
  return { feeds: [feed()], max_feeds: 10, can_add: true, ...overrides };
}

function mount(data: SharingSettings | null, extra: Record<string, unknown> = {}) {
  const props = { settings: data, onReload: vi.fn(), onSaved: vi.fn(), onError: vi.fn(), ...extra };
  render(SharingCard, props);
  return props;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('lists each feed by name, id and host, with a failing one explained', () => {
  mount(
    settings({
      feeds: [
        feed(),
        feed({
          id: 13,
          name: null,
          host: 'other.example',
          state: 'error',
          last_reason: 'http_404',
        }),
      ],
    }),
  );
  const rows = screen.getAllByRole('listitem');
  expect(rows[0]).toHaveTextContent('Work');
  expect(rows[0]).toHaveTextContent('feed:12 · cal.example.org');
  expect(rows[0]).toHaveTextContent('Active');
  expect(rows[1]).toHaveTextContent('other.example');
  expect(rows[1]).toHaveTextContent('Not refreshing');
  expect(rows[1]).toHaveTextContent('The calendar answered with an error (404)');
});

it('renders a calendar name as text, never markup', () => {
  mount(settings({ feeds: [feed({ name: '<img src=x onerror=alert(1)>' })] }));
  expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
  expect(document.querySelector('img')).toBeNull();
});

it('adds a calendar by its address and reloads', async () => {
  vi.mocked(addSharedFeed).mockResolvedValue({ feed: feed() });
  const props = mount(settings({ feeds: [] }));
  expect(screen.getByText('No calendars shared with Istota.')).toBeInTheDocument();
  await fireEvent.click(screen.getByRole('button', { name: 'Add calendar' }));
  const dialog = await screen.findByRole('dialog');
  await fireEvent.input(within(dialog).getByLabelText('Calendar address'), {
    target: { value: '  webcal://cal.example.org/secret.ics ' },
  });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Add calendar' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(addSharedFeed).toHaveBeenCalledWith('webcal://cal.example.org/secret.ics');
});

it('keeps a refused address in the dialog and says why', async () => {
  vi.mocked(addSharedFeed).mockRejectedValueOnce(
    new Error('That address did not return a calendar.'),
  );
  const props = mount(settings());
  await fireEvent.click(screen.getByRole('button', { name: 'Add calendar' }));
  const dialog = await screen.findByRole('dialog');
  const input = within(dialog).getByLabelText('Calendar address');
  await fireEvent.input(input, { target: { value: 'https://cal.example.org/login' } });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Add calendar' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'That address did not return a calendar.',
  );
  expect(input).toHaveValue('https://cal.example.org/login');
  expect(props.onSaved).not.toHaveBeenCalled();
});

it('removes a calendar after confirming', async () => {
  vi.mocked(removeSharedFeed).mockResolvedValue({ removed: true });
  const props = mount(settings());
  await fireEvent.click(screen.getByRole('button', { name: 'Actions for Work' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove' }));
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('Are you sure you want to remove Work?');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(removeSharedFeed).toHaveBeenCalledWith(12);
});

it('hands an expired session to onError', async () => {
  vi.mocked(removeSharedFeed).mockRejectedValueOnce(new AuthError());
  const props = mount(settings());
  await fireEvent.click(screen.getByRole('button', { name: 'Actions for Work' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove' }));
  const dialog = await screen.findByRole('dialog');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await waitFor(() => expect(props.onError).toHaveBeenCalledOnce());
});

it('disables adding without a credential store, and at the limit', () => {
  mount(settings({ can_add: false }));
  expect(screen.getByRole('button', { name: 'Add calendar' })).toBeDisabled();
  expect(screen.getByText(/credential store is set up/)).toBeInTheDocument();
  cleanup();
  mount(settings({ max_feeds: 1 }));
  expect(screen.getByRole('button', { name: 'Add calendar' })).toBeDisabled();
  expect(screen.getByText('This server allows 1 shared calendars per person.')).toBeInTheDocument();
});

it('shows a first load that failed with a retry', async () => {
  const props = mount(null, { loadError: 'The shared calendars could not be loaded' });
  expect(screen.getByRole('alert')).toHaveTextContent('The shared calendars could not be loaded');
  await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(props.onReload).toHaveBeenCalledOnce();
});
