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
  acceptSharedFolder,
  addSharedFeed,
  createCalendarFeedLink,
  declineSharedFolder,
  removeCalendarFeedLink,
  removeSharedFeed,
  removeSharedFolder,
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

// --- Istota's own calendar -----------------------------------------------------

function withCalendar(
  overrides: Partial<NonNullable<SharingSettings['calendar']>> = {},
): SharingSettings {
  return settings({
    calendar: {
      delivery: 'invite',
      effective_delivery: 'invite',
      email_enabled: true,
      feed: null,
      can_link: true,
      reachable_elsewhere: true,
      ...overrides,
    },
  });
}

const MADE = { created_at: '2026-10-20T10:00:00Z', last_used_at: null };

it('shows the feed address once, when it is made', async () => {
  const url = 'https://bot.example.com/istota/calendar/' + 'A'.repeat(43) + '.ics';
  vi.mocked(createCalendarFeedLink).mockResolvedValue({ url });
  const props = mount(withCalendar());
  await fireEvent.click(screen.getByRole('button', { name: 'Get feed address' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(screen.getByLabelText('Calendar feed address')).toHaveValue(url);
  expect(screen.getByText(/Shown this once/)).toBeInTheDocument();
});

it('resets an existing address only after confirming', async () => {
  vi.mocked(createCalendarFeedLink).mockResolvedValue({ url: 'https://x.example/new.ics' });
  const props = mount(withCalendar({ feed: MADE }));
  expect(screen.queryByRole('button', { name: 'Get feed address' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Reset address' }));
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('stops working at once');
  expect(createCalendarFeedLink).not.toHaveBeenCalled();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Reset' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(createCalendarFeedLink).toHaveBeenCalledOnce();
});

it('removes the address after confirming', async () => {
  vi.mocked(removeCalendarFeedLink).mockResolvedValue({ removed: true });
  const props = mount(withCalendar({ feed: MADE }));
  await fireEvent.click(screen.getByRole('button', { name: 'Remove address' }));
  const dialog = await screen.findByRole('dialog');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(removeCalendarFeedLink).toHaveBeenCalledOnce();
});

it('says invitations and the feed duplicate, and that a local name is local', () => {
  mount(withCalendar({ reachable_elsewhere: false }));
  expect(screen.getByText(/each event shows twice/)).toBeInTheDocument();
  expect(screen.getByText(/only calendar apps on this computer/)).toBeInTheDocument();
});

it('without email the events go by the feed', () => {
  mount(withCalendar({ email_enabled: false, effective_delivery: 'feed' }));
  expect(screen.getByText(/sends no email/)).toBeInTheDocument();
  expect(screen.queryByText(/each event shows twice/)).toBeNull();
});

it('without a public name there is no address to get', () => {
  mount(withCalendar({ can_link: false }));
  expect(screen.queryByRole('button', { name: 'Get feed address' })).toBeNull();
  expect(screen.getByText(/no public name/)).toBeInTheDocument();
});

it('a failed address request goes to onError and shows no address', async () => {
  vi.mocked(createCalendarFeedLink).mockRejectedValueOnce(new Error('nope'));
  const props = mount(withCalendar());
  await fireEvent.click(screen.getByRole('button', { name: 'Get feed address' }));
  await waitFor(() => expect(props.onError).toHaveBeenCalledOnce());
  expect(screen.queryByLabelText('Calendar feed address')).toBeNull();
});

it('a server with no calendar section draws none', () => {
  mount(settings());
  expect(screen.queryByText("Istota's calendar")).toBeNull();
});

// --- Folders shared over OCM ---------------------------------------------------

function withFolders(
  pending: NonNullable<SharingSettings['ocm']>['pending'] = [],
): SharingSettings {
  return settings({ feeds: [], ocm: { federated_id: 'alice@bot.example.com', pending } });
}

const OFFER = {
  id: 7,
  name: 'Taxes 2026',
  owner: 'carol@https://cloud.example.org',
  host: 'cloud.example.org',
  reoffer: false,
  created_at: '2026-10-25T10:00:00Z',
};

it('shows the federated address to copy while OCM is on', () => {
  mount(withFolders());
  expect(screen.getByLabelText('Federated sharing address')).toHaveValue('alice@bot.example.com');
  expect(screen.getByText('No folder offers are waiting.')).toBeInTheDocument();
});

it('hides the address while OCM is off', () => {
  mount(settings({ ocm: null }));
  expect(screen.queryByText('Shared folders')).toBeNull();
  expect(screen.queryByLabelText('Federated sharing address')).toBeNull();
});

it('lists pending offers, the sender words as text', () => {
  mount(
    withFolders([
      OFFER,
      { ...OFFER, id: 8, name: '<b>Plans</b>', owner: null, host: 'other.example', reoffer: true },
    ]),
  );
  const list = screen.getByRole('list', { name: 'Folder offers' });
  const rows = within(list).getAllByRole('listitem');
  expect(rows[0]).toHaveTextContent('Taxes 2026');
  expect(rows[0]).toHaveTextContent('from carol@https://cloud.example.org · cloud.example.org');
  expect(rows[0]).toHaveTextContent('Waiting');
  expect(rows[1]).toHaveTextContent('<b>Plans</b>');
  expect(rows[1]).toHaveTextContent('Offered again');
  expect(document.querySelector('b')).toBeNull();
  expect(within(rows[0]).getByRole('button', { name: 'Accept Taxes 2026' })).toBeEnabled();
  expect(within(rows[0]).getByRole('button', { name: 'Decline Taxes 2026' })).toBeInTheDocument();
});

const SHARE = {
  id: 7,
  name: 'Taxes 2026',
  owner: 'carol@https://cloud.example.org',
  host: 'cloud.example.org',
  access: 'write' as const,
  state: 'active' as const,
  last_reason: null,
  created_at: '2026-10-25T10:00:00Z',
};

it('accepts with scheduled use off unless ticked, then reloads', async () => {
  vi.mocked(acceptSharedFolder).mockResolvedValue({ share: SHARE, notified: true });
  const props = mount(withFolders([OFFER]));
  await fireEvent.click(screen.getByRole('button', { name: 'Accept Taxes 2026' }));
  const dialog = await screen.findByRole('dialog');
  const box = within(dialog).getByRole('checkbox', { name: 'Let scheduled jobs use it' });
  expect(box).not.toBeChecked();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Accept' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(acceptSharedFolder).toHaveBeenCalledWith(7, false);

  cleanup();
  vi.clearAllMocks();
  vi.mocked(acceptSharedFolder).mockResolvedValue({ share: SHARE, notified: true });
  mount(withFolders([OFFER]));
  await fireEvent.click(screen.getByRole('button', { name: 'Accept Taxes 2026' }));
  const again = await screen.findByRole('dialog');
  await fireEvent.click(within(again).getByRole('checkbox', { name: 'Let scheduled jobs use it' }));
  await fireEvent.click(within(again).getByRole('button', { name: 'Accept' }));
  await waitFor(() => expect(acceptSharedFolder).toHaveBeenCalledWith(7, true));
});

it('keeps a refused accept in the dialog and says why', async () => {
  vi.mocked(acceptSharedFolder).mockRejectedValueOnce(new Error('This share could not be opened.'));
  const props = mount(withFolders([OFFER]));
  await fireEvent.click(screen.getByRole('button', { name: 'Accept Taxes 2026' }));
  const dialog = await screen.findByRole('dialog');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Accept' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'This share could not be opened.',
  );
  expect(props.onSaved).not.toHaveBeenCalled();
});

it('declines only after confirming', async () => {
  vi.mocked(declineSharedFolder).mockResolvedValue({ notified: true });
  const props = mount(withFolders([OFFER]));
  await fireEvent.click(screen.getByRole('button', { name: 'Decline Taxes 2026' }));
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('Are you sure you want to decline Taxes 2026?');
  expect(declineSharedFolder).not.toHaveBeenCalled();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Decline' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(declineSharedFolder).toHaveBeenCalledWith(7);
});

it('says why accepting is unavailable, and disables it', () => {
  const data = withFolders([OFFER]);
  data.ocm!.accept_refusal = 'Credentials need a sandboxed server here.';
  mount(data);
  expect(screen.getByText('Credentials need a sandboxed server here.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Accept Taxes 2026' })).toBeDisabled();
});

it('lists accepted shares and removes one after confirming', async () => {
  vi.mocked(removeSharedFolder).mockResolvedValue({ notified: true });
  const data = withFolders([]);
  data.ocm!.shares = [SHARE, { ...SHARE, id: 8, name: '<i>Old</i>', access: 'read' }];
  const props = mount(data);
  expect(screen.queryByText('No folder offers are waiting.')).toBeNull();
  const rows = within(screen.getByRole('list', { name: 'Shared folders' })).getAllByRole(
    'listitem',
  );
  expect(rows[0]).toHaveTextContent('from carol@https://cloud.example.org · cloud.example.org');
  expect(rows[0]).toHaveTextContent('share:7');
  expect(rows[0]).toHaveTextContent('Read and write');
  expect(rows[1]).toHaveTextContent('<i>Old</i>');
  expect(rows[1]).toHaveTextContent('Read only');
  expect(document.querySelector('i')).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Actions for Taxes 2026' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove' }));
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('Are you sure you want to remove Taxes 2026?');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(removeSharedFolder).toHaveBeenCalledWith(7);
});

it('with sharing off, keeps the shares removable and drops the address', () => {
  mount(
    settings({
      feeds: [],
      ocm: { enabled: false, federated_id: null, pending: [], shares: [SHARE] },
    }),
  );
  expect(screen.queryByLabelText('Federated sharing address')).toBeNull();
  expect(screen.getByText(/no longer receives folder shares/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Actions for Taxes 2026' })).toBeInTheDocument();
});
