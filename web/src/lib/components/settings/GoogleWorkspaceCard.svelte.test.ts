/**
 * The Google card has to keep four states apart that the old Connected pill
 * collapsed into one (ISSUE-240): the instance does not offer a service, the
 * user did not grant it, they granted it at some level, and their grant no
 * longer matches what a reconnect would ask for.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { GoogleStatus } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';

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
    unoffered_scopes: [],
    selection: { drive: 'full', gmail: 'readonly', chat: 'off' },
    selection_set: false,
    requested_scopes: [DRIVE_RO],
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

  it('reports what Google holds on the service row itself', async () => {
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
            also: [],
          },
        ],
      }),
    );
    expect(await screen.findByText('Connected')).toBeInTheDocument();
    expect(screen.getByText('granted: Read-only')).toBeInTheDocument();
  });

  /**
   * The level used to be stated twice — once in a "What you granted" list and
   * again beside the picker — which is what the two section headings were
   * separating. One statement per service means neither heading has anything
   * to head, so both went with the list.
   */
  it('states a granted level once, under no section heading', async () => {
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
            also: [],
          },
        ],
      }),
    );
    await screen.findByText('granted: Read-only');
    expect(screen.getAllByText(/granted: Read-only/)).toHaveLength(1);
    expect(screen.queryByText(/what you granted/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/what to ask for/i)).not.toBeInTheDocument();
    expect(document.querySelector('h3')).toBeNull();
  });

  /**
   * Beside the title, like every other service card's — not a Badge two lines
   * into the body, below the description it qualifies.
   */
  it('puts the connection pill in the card header', async () => {
    await mount(status({ connected: true }));
    const pill = await screen.findByText('Connected');
    expect(pill).toHaveClass('status-pill');
    expect(pill.closest('.section-header')).not.toBeNull();
  });

  it('withholds the pill while the status is still loading', () => {
    api.getGoogleStatus.mockReturnValue(new Promise(() => {}));
    render(GoogleWorkspaceCard, {});
    expect(screen.queryByText('Not connected')).not.toBeInTheDocument();
  });

  it('reports no connection state on an instance with Google switched off', async () => {
    await mount(status({ enabled: false }));
    expect(screen.queryByText('Not connected')).not.toBeInTheDocument();
    expect(screen.queryByText('Connected')).not.toBeInTheDocument();
  });

  it('flags a partial grant rather than showing it as complete', async () => {
    await mount(
      status({
        connected: true,
        granted: [
          {
            service: 'chat',
            label: 'Chat',
            level: 'readonly',
            scopes: [],
            complete: false,
            also: [],
          },
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

  it('shows a granted scope of a lower level rather than hiding it', async () => {
    await mount(
      status({
        connected: true,
        granted: [
          {
            service: 'chat',
            label: 'Chat',
            level: 'full',
            scopes: ['https://www.googleapis.com/auth/chat.spaces'],
            complete: false,
            also: ['https://www.googleapis.com/auth/chat.messages.readonly'],
          },
        ],
      }),
    );
    expect(
      await screen.findByText('https://www.googleapis.com/auth/chat.messages.readonly'),
    ).toBeInTheDocument();
  });

  it('names the ceiling scopes no picker row covers', async () => {
    await mount(status({ unoffered_scopes: ['https://www.googleapis.com/auth/gmail.send'] }));
    expect(
      await screen.findByText('https://www.googleapis.com/auth/gmail.send'),
    ).toBeInTheDocument();
  });

  it('withholds Connect when the saved selection would request nothing', async () => {
    await mount(status({ requested_scopes: [] }));
    expect(await screen.findByRole('button', { name: 'Connect' })).toBeDisabled();
    expect(screen.getByText('Choose at least one service before connecting.')).toBeInTheDocument();
  });

  it('withholds Reconnect on the same grounds', async () => {
    await mount(status({ connected: true, requested_scopes: [] }));
    expect(await screen.findByRole('button', { name: 'Reconnect' })).toBeDisabled();
  });

  describe('saving the selection', () => {
    /**
     * The card owns no button — it registers with the app bar's shared Save,
     * which asks only the contributors holding edits. So a save test has to
     * make the picker dirty first, through the control the user uses.
     */
    async function pickLevel(field: string, optionLabel: string) {
      const trigger = await screen.findByLabelText(field);
      await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
      await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
      await fireEvent.click(trigger);
      const option = await screen.findByRole('option', { name: optionLabel });
      await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
      await fireEvent.click(option);
    }

    async function headerSave() {
      const aggregate = get(settingsSave);
      expect(aggregate).not.toBeNull();
      await aggregate!.save();
    }

    it('is not dirty until a level actually changes', async () => {
      await mount(status());
      expect(get(settingsSave)?.dirty).toBe(false);
    });

    it('goes dirty once a level changes', async () => {
      await mount(status());
      await pickLevel('Drive access level', 'Read-only');
      await waitFor(() => expect(get(settingsSave)?.dirty).toBe(true));
    });

    it('writes the pending selection and reports a needed reconnect', async () => {
      await mount(status({ connected: true }));
      api.saveGoogleScopes.mockResolvedValue({
        ok: true,
        selection: { drive: 'readonly', gmail: 'readonly', chat: 'off' },
        requested_scopes: [DRIVE_RO],
        reconnect_required: true,
      });
      await pickLevel('Drive access level', 'Read-only');
      await headerSave();
      expect(api.saveGoogleScopes).toHaveBeenCalledWith({
        drive: 'readonly',
        gmail: 'readonly',
        chat: 'off',
      });
      expect(
        await screen.findByText(/Reconnect your Google account to grant the new access/i),
      ).toBeInTheDocument();
    });

    it('says only "Saved." when the existing grant already covers it', async () => {
      await mount(status({ connected: true }));
      api.saveGoogleScopes.mockResolvedValue({
        ok: true,
        selection: { drive: 'readonly', gmail: 'readonly', chat: 'off' },
        requested_scopes: [DRIVE_RO],
        reconnect_required: false,
      });
      await pickLevel('Drive access level', 'Read-only');
      await headerSave();
      expect(await screen.findByText('Saved.')).toBeInTheDocument();
    });

    it('reports a save failure against the card', async () => {
      await mount(status());
      api.saveGoogleScopes.mockRejectedValue(new Error('nope'));
      await pickLevel('Drive access level', 'Read-only');
      await headerSave();
      expect(await screen.findByText('nope')).toBeInTheDocument();
    });
  });
});
