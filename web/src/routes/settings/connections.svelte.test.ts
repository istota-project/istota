import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
  getSettingsServices: vi.fn(async () => ({ services: [] })),
});
import { AuthError } from '$lib/api';
import Harness from './connectionsHarness.test.svelte';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const listing = {
  enabled: true,
  allow_send: true,
  max_per_user: 10,
  refusal: null,
  accounts: [],
};

it('draws the mail accounts card when the listing answers, with no services', async () => {
  api.getMailAccounts.mockResolvedValue(listing);
  render(Harness);
  expect(await screen.findByRole('heading', { name: 'Mail accounts' })).toBeInTheDocument();
  expect(screen.getByText('No mail accounts connected.')).toBeInTheDocument();
  expect(screen.queryByText(/No connected services are available/)).toBeNull();
});

it('draws no card when the operator has switched mail accounts off', async () => {
  api.getMailAccounts.mockRejectedValue(Object.assign(new Error('Not found'), { status: 404 }));
  render(Harness);
  expect(await screen.findByText(/No connected services are available/)).toBeInTheDocument();
  await waitFor(() => expect(api.getMailAccounts).toHaveBeenCalledOnce());
  expect(screen.queryByRole('heading', { name: 'Mail accounts' })).toBeNull();
});

it('shows a failed first load in the card', async () => {
  api.getMailAccounts.mockRejectedValue(
    Object.assign(new Error('The mail accounts could not be loaded'), { status: 500 }),
  );
  render(Harness);
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'The mail accounts could not be loaded',
  );
});

it("draws a skill plugin's secret service as a card", async () => {
  api.getMailAccounts.mockRejectedValue(Object.assign(new Error('Not found'), { status: 404 }));
  api.getSettingsServices.mockResolvedValue({
    services: [
      {
        service: 'plugin_example',
        label: 'plugin_example',
        status: 'configured',
        fields: [
          { key: 'token', label: 'token', type: 'password' },
          { key: 'url', label: 'url', type: 'text', plain: true },
        ],
        configured_keys: ['token', 'url'],
        values: { url: 'https://example.com' },
        last_updated: null,
        used_by: ['example'],
        hint: 'Credentials for a skill plugin (example-plugin 0.1.0).',
        oauth: false,
        custom_ui: false,
      },
    ],
  });
  render(Harness);

  expect(await screen.findByRole('heading', { name: 'plugin_example' })).toBeInTheDocument();
  expect(screen.getByText('Credentials for a skill plugin (example-plugin 0.1.0).')).toBeVisible();
  expect(screen.getByText('Configured')).toBeInTheDocument();
  // The stored token is never sent back, so its field starts empty; the plain
  // field shows what is stored.
  expect((screen.getByLabelText('token') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('url') as HTMLInputElement).value).toBe('https://example.com');
  expect(screen.getAllByTitle('Clear stored value').length).toBe(2);
  expect(screen.queryByText(/No connected services are available/)).toBeNull();
});

it('ends an expired session', async () => {
  api.getMailAccounts.mockRejectedValue(new AuthError());
  const expireSession = vi.fn();
  render(Harness, { expireSession });
  await waitFor(() => expect(expireSession).toHaveBeenCalledOnce());
});
