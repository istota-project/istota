import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { AdminConnection, AdminGoogleWorkspaceConnection } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
vi.mock('$app/paths', () => ({ base: '/istota' }));

import Page from './+page.svelte';

function connection(
  over: Partial<AdminGoogleWorkspaceConnection> = {},
): AdminGoogleWorkspaceConnection {
  return {
    id: 'google_workspace',
    label: 'Google Workspace',
    enabled: true,
    client_id: 'example.apps.googleusercontent.com',
    client_secret_set: true,
    redirect_uri: 'https://server.example.com/istota/google/callback',
    services: [
      { service: 'drive', label: 'Drive', max_level: 'readonly' },
      { service: 'gmail', label: 'Gmail', max_level: 'full' },
      { service: 'calendar', label: 'Calendar', max_level: 'off' },
      { service: 'sheets', label: 'Sheets', max_level: 'off' },
      { service: 'docs', label: 'Docs', max_level: 'off' },
      { service: 'contacts', label: 'Contacts', max_level: 'off' },
      { service: 'chat', label: 'Chat', max_level: 'off' },
    ],
    additional_scopes: [],
    missing: [],
    status: 'configured',
    ...over,
  };
}

function olderConnections(): AdminConnection[] {
  return [
    {
      id: 'nextcloud',
      label: 'Nextcloud',
      configured: false,
      url: '',
      username: '',
      talk_enabled: false,
      talk_bot_username: '',
      storage: 'local',
      check: null,
    },
    {
      id: 'whatsapp',
      label: 'WhatsApp',
      enabled: false,
      provider: 'baileys',
      number: '',
      pairing_supported: true,
      pairing_enabled: false,
      restart_interval_seconds: 0,
      credential_errors: [],
      link: null,
      pairing: null,
      pairing_blocked_reason: null,
    },
  ];
}

async function mount(over: Partial<AdminGoogleWorkspaceConnection> = {}) {
  api.getAdminConnections.mockResolvedValue({ connections: [connection(over)] });
  render(Page);
  return within(await screen.findByTestId('google-workspace-card'));
}

beforeEach(() => {
  api.getAdminConnections.mockReset();
});
afterEach(cleanup);

describe('the admin Google Workspace card', () => {
  it.each([
    ['disabled', 'Disabled'],
    ['incomplete', 'Setup incomplete'],
    ['configured', 'Configured'],
  ] as const)('shows %s with configuration actions and account guidance', async (status, label) => {
    const q = await mount({ status, enabled: status !== 'disabled' });
    expect(q.getByText(label)).toBeTruthy();
    expect(q.getByRole('link', { name: 'Configure' }).getAttribute('href')).toBe(
      '/istota/admin/config?section=google_workspace',
    );
    expect(q.getByRole('link', { name: 'Setup guide' }).getAttribute('href')).toBe(
      'https://istota.cynium.com/docs/features/google-workspace/',
    );
    expect(
      q.getByText(/Each user connects their own Google account in Settings → Connections/),
    ).toBeTruthy();
    expect(
      q.getByText(/does not verify credentials, enabled APIs or Google consent settings/),
    ).toBeTruthy();
    expect(q.queryByRole('button', { name: /connect/i })).toBeNull();
    expect(q.queryByText('Connected')).toBeNull();
  });

  it('shows credentials presence, callback and every maximum service level', async () => {
    const q = await mount();
    expect(q.getByText('example.apps.googleusercontent.com')).toBeTruthy();
    expect(q.getByText('Set')).toBeTruthy();
    const callback = q.getByText('https://server.example.com/istota/google/callback');
    expect(callback.closest('a')).toBeNull();
    expect(q.getByText('Maximum service access')).toBeTruthy();
    for (const service of connection().services) expect(q.getByText(service.label)).toBeTruthy();
    expect(q.getByText('Read-only')).toBeTruthy();
    expect(q.getByText('Read and write')).toBeTruthy();
    expect(q.getAllByText('No access')).toHaveLength(5);
    expect(q.queryByText('No services are allowed')).toBeNull();
  });

  it('renders the client ID and callback as inert text', async () => {
    const clientId = '<img src=x onerror=alert(1)>';
    const callback = 'javascript:alert(1)';
    const q = await mount({ client_id: clientId, redirect_uri: callback });
    expect(q.getByText(clientId).closest('a')).toBeNull();
    expect(q.getByText(callback).closest('a')).toBeNull();
    expect(q.queryByRole('img')).toBeNull();
    expect(q.queryByRole('textbox')).toBeNull();
  });

  it('explains missing setup and sends the hostname correction to deployment setup', async () => {
    const q = await mount({
      status: 'incomplete',
      client_id: '',
      client_secret_set: false,
      redirect_uri: null,
      missing: ['client_id', 'client_secret', 'site_hostname', 'scopes'],
      services: connection().services.map((service) => ({ ...service, max_level: 'off' })),
    });
    expect(q.getAllByText('Not set')).toHaveLength(3);
    expect(q.getByText(/Set the OAuth client ID/)).toBeTruthy();
    expect(q.getByText(/Set the OAuth client secret/)).toBeTruthy();
    expect(
      q.getByText(/Set the site hostname through the deployment setup procedure/),
    ).toBeTruthy();
    expect(q.getByText(/Allow at least one Google scope/)).toBeTruthy();
    expect(q.getByText('No services are allowed')).toBeTruthy();
    expect(q.getAllByText('No access')).toHaveLength(7);
  });

  it('keeps an additional-only scope visible without claiming that no access is allowed', async () => {
    const q = await mount({
      services: connection().services.map((service) => ({ ...service, max_level: 'off' })),
      additional_scopes: ['https://www.googleapis.com/auth/drive.file'],
    });
    expect(q.getByText('Configured')).toBeTruthy();
    expect(q.getByText('Additional scopes requested on connection')).toBeTruthy();
    expect(q.getByText('https://www.googleapis.com/auth/drive.file')).toBeTruthy();
    expect(q.getAllByText('No access')).toHaveLength(7);
    expect(q.queryByText('No services are allowed')).toBeNull();
  });

  it('places Google after Nextcloud and WhatsApp through the index page', async () => {
    api.getAdminConnections.mockResolvedValue({
      connections: [...olderConnections(), connection()],
    });
    render(Page);
    await screen.findByTestId('google-workspace-card');
    expect(screen.getAllByRole('heading', { level: 2 }).map((node) => node.textContent)).toEqual([
      'Nextcloud',
      'WhatsApp',
      'Google Workspace',
    ]);
  });

  it('still renders older index responses without Google', async () => {
    api.getAdminConnections.mockResolvedValue({ connections: olderConnections() });
    render(Page);
    await screen.findByTestId('nextcloud-card');
    expect(screen.getByRole('heading', { name: 'WhatsApp' })).toBeTruthy();
    expect(screen.queryByTestId('google-workspace-card')).toBeNull();
  });

  it('keeps the existing index failure visible', async () => {
    api.getAdminConnections.mockRejectedValue(new Error('API error: 503'));
    render(Page);
    expect(await screen.findByText('API error: 503')).toBeTruthy();
    expect(screen.queryByTestId('google-workspace-card')).toBeNull();
  });
});
