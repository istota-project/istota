/**
 * The Google card has to keep four states apart that the old Connected pill
 * collapsed into one (ISSUE-240): the instance does not offer a service, the
 * user did not grant it, they granted it at some level, and their grant no
 * longer matches what a reconnect would ask for.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import type { GoogleStatus } from '$lib/api';

const api = vi.hoisted(() => ({
  getGoogleStatus: vi.fn(),
  saveGoogleScopes: vi.fn(),
  disconnectGoogle: vi.fn(),
}));
vi.mock('$lib/api', () => api);

import GoogleWorkspaceCard from './GoogleWorkspaceCard.svelte';

const DRIVE_RO = 'https://www.googleapis.com/auth/drive.readonly';
const GMAIL_RO = 'https://www.googleapis.com/auth/gmail.readonly';

function status(over: Partial<GoogleStatus> = {}): GoogleStatus {
  return {
    enabled: true,
    connected: false,
    offered: [
      { service: 'drive', label: 'Drive', max_level: 'full' },
      { service: 'gmail', label: 'Gmail', max_level: 'readonly' },
      { service: 'chat', label: 'Chat', max_level: 'off' },
    ],
    granted: [],
    unrecognized_scopes: [],
    selection: { drive: 'full', gmail: 'readonly', chat: 'off' },
    selection_set: false,
    requested_scopes: [],
    missing_scopes: [],
    extra_scopes: [],
    ...over,
  };
}

beforeEach(() => {
  api.getGoogleStatus.mockReset();
  api.saveGoogleScopes.mockReset();
  api.disconnectGoogle.mockReset();
});

afterEach(cleanup);

async function mount(s: GoogleStatus) {
  api.getGoogleStatus.mockResolvedValue(s);
  render(GoogleWorkspaceCard, {});
  await waitFor(() => expect(api.getGoogleStatus).toHaveBeenCalled());
  await screen.findByText('Google Workspace');
}

describe('GoogleWorkspaceCard', () => {
  it('says so when the instance has Google switched off', async () => {
    await mount(status({ enabled: false }));
    expect(await screen.findByText(/not configured on this Istota instance/i)).toBeInTheDocument();
  });

  it('shows Not connected before a grant exists', async () => {
    await mount(status());
    expect(await screen.findByText('Not connected')).toBeInTheDocument();
  });

  it('renders the granted services with their level', async () => {
    await mount(
      status({
        connected: true,
        granted: [
          {
            service: 'drive',
            label: 'Drive',
            level: 'readonly',
            scopes: [DRIVE_RO],
            complete: true,
          },
        ],
      }),
    );
    expect(await screen.findByText('Connected')).toBeInTheDocument();
    expect(screen.getAllByText('Read-only').length).toBeGreaterThan(0);
  });

  it('flags a partial grant rather than showing it as complete', async () => {
    await mount(
      status({
        connected: true,
        granted: [
          { service: 'chat', label: 'Chat', level: 'readonly', scopes: [], complete: false },
        ],
      }),
    );
    expect(await screen.findByText(/some boxes were deselected/i)).toBeInTheDocument();
  });

  it('shows an unrecognised granted scope verbatim instead of dropping it', async () => {
    await mount(
      status({
        connected: true,
        unrecognized_scopes: ['https://www.googleapis.com/auth/tasks'],
      }),
    );
    expect(await screen.findByText('https://www.googleapis.com/auth/tasks')).toBeInTheDocument();
  });

  it('tells the user to reconnect when the grant is narrower than the request', async () => {
    await mount(
      status({
        connected: true,
        requested_scopes: [DRIVE_RO, GMAIL_RO],
        missing_scopes: [GMAIL_RO],
      }),
    );
    expect(await screen.findByText('Reconnect needed')).toBeInTheDocument();
    expect(screen.getByText(/narrower than what this instance now asks for/i)).toBeInTheDocument();
  });

  it('reports a grant wider than the current selection', async () => {
    await mount(status({ connected: true, extra_scopes: [GMAIL_RO] }));
    expect(await screen.findByText(/wider than the current selection/i)).toBeInTheDocument();
  });

  it('marks a service the instance does not offer', async () => {
    await mount(status());
    expect(
      await screen.findByText('This instance does not offer this service.'),
    ).toBeInTheDocument();
  });

  it('offers a picker row per service', async () => {
    await mount(status());
    for (const label of ['Drive access level', 'Gmail access level', 'Chat access level']) {
      expect(await screen.findByLabelText(label)).toBeInTheDocument();
    }
  });

  it('disables the picker for a service above the ceiling', async () => {
    await mount(status());
    expect(await screen.findByLabelText('Chat access level')).toBeDisabled();
    expect(screen.getByLabelText('Drive access level')).not.toBeDisabled();
  });

  it('offers Connect when disconnected and Reconnect/Disconnect when connected', async () => {
    await mount(status());
    expect(await screen.findByRole('button', { name: 'Connect' })).toBeInTheDocument();
    cleanup();
    await mount(status({ connected: true }));
    expect(await screen.findByRole('button', { name: 'Reconnect' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
  });

  it('surfaces a status load failure instead of rendering an empty picker', async () => {
    api.getGoogleStatus.mockRejectedValue(new Error('boom'));
    render(GoogleWorkspaceCard, {});
    expect(await screen.findByText('boom')).toBeInTheDocument();
  });
});
