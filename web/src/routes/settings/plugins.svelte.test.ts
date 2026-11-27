import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import { AuthError, type PluginsResponse } from '$lib/api';
import { USER_SETTINGS_SECTIONS } from '$lib/settings/sections';
import Harness from './pluginsHarness.test.svelte';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const listing: PluginsResponse = {
  plugins: [
    {
      skill: 'example',
      description: 'Look things up in the example service.',
      plugin: 'example-plugin 0.1.0',
      disabled: false,
    },
    {
      skill: 'weather_notes',
      description: 'Keep a note of the weather each morning.',
      plugin: 'weather-notes 1.2.0',
      disabled: true,
    },
  ],
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
};

it('sits after Connections in the sections', () => {
  const hrefs = USER_SETTINGS_SECTIONS.map((section) => section.href);
  expect(hrefs.indexOf('/plugins')).toBe(hrefs.indexOf('/connections') + 1);
  expect(USER_SETTINGS_SECTIONS.find((s) => s.href === '/plugins')?.label).toBe('Plugins');
});

it('says so when the installation has no plugins', async () => {
  api.getSettingsPlugins.mockResolvedValue({ plugins: [], services: [] });
  render(Harness);

  expect(
    await screen.findByText('No plugins are available on this installation.'),
  ).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Installed plugins' })).toBeNull();
});

it('lists each plugin with its package and marks one the user disabled', async () => {
  api.getSettingsPlugins.mockResolvedValue(listing);
  render(Harness);

  expect(await screen.findByRole('heading', { name: 'Installed plugins' })).toBeInTheDocument();
  expect(screen.getByText('weather_notes')).toBeInTheDocument();
  expect(screen.getByText('Keep a note of the weather each morning.')).toBeInTheDocument();
  expect(screen.getByText('example-plugin 0.1.0')).toBeInTheDocument();
  expect(screen.getByText('weather-notes 1.2.0')).toBeInTheDocument();
  expect(screen.getAllByText('Disabled').length).toBe(1);
  expect(screen.getByRole('link', { name: 'Preferences' }).getAttribute('href')).toBe(
    '/settings/preferences',
  );
  expect(screen.queryByText(/No plugins are available/)).toBeNull();
});

it("draws a plugin's secret service as a card", async () => {
  api.getSettingsPlugins.mockResolvedValue(listing);
  render(Harness);

  expect(await screen.findByRole('heading', { name: 'plugin_example' })).toBeInTheDocument();
  expect(screen.getByText('Credentials for a skill plugin (example-plugin 0.1.0).')).toBeVisible();
  expect(screen.getByText('Configured')).toBeInTheDocument();
  // The stored token is never sent back, so its field starts empty; the plain
  // field shows what is stored.
  expect((screen.getByLabelText('token') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('url') as HTMLInputElement).value).toBe('https://example.com');
  expect(screen.getAllByTitle('Clear stored value').length).toBe(2);
});

it('lists a plugin that needs no credentials without a card', async () => {
  api.getSettingsPlugins.mockResolvedValue({ plugins: [listing.plugins[0]], services: [] });
  render(Harness);

  expect(await screen.findByRole('heading', { name: 'Installed plugins' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'plugin_example' })).toBeNull();
  expect(screen.queryByText('Disabled')).toBeNull();
});

it('shows a failed first load in the pane, with a way to try again', async () => {
  api.getSettingsPlugins.mockRejectedValueOnce(new Error('The plugins could not be loaded.'));
  api.getSettingsPlugins.mockResolvedValue({ plugins: [], services: [] });
  render(Harness);

  expect(await screen.findByText('The plugins could not be loaded.')).toBeInTheDocument();
  screen.getByRole('button', { name: 'Retry' }).click();
  expect(
    await screen.findByText('No plugins are available on this installation.'),
  ).toBeInTheDocument();
});

it('ends an expired session', async () => {
  api.getSettingsPlugins.mockRejectedValue(new AuthError());
  const expireSession = vi.fn();
  render(Harness, { expireSession });
  await waitFor(() => expect(expireSession).toHaveBeenCalledOnce());
});
