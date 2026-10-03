/**
 * The briefing "Send as HTML" toggle writes `briefing_email_html` through the
 * profile PUT, which refuses an edit to a field the deployment manages. A
 * managed toggle renders locked, like the fields on /settings.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import type { User } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const managed = vi.hoisted(() => ({ value: [] as string[] }));
await fillApiDouble(api, {
  getBriefings: vi.fn(async () => ({ briefings: [], rooms: [], outputs: ['email'] })),
  getBrowsePresets: vi.fn(async () => ({ presets: [] })),
  getFeedOptions: vi.fn(async () => ({ available: false, subscriptions: [], categories: [] })),
  getBriefingConfig: vi.fn(async () => ({
    briefings: [],
    schedule_names: [],
    source_kinds: [],
    structured_kinds: [],
  })),
  getBriefingPathSuggestions: vi.fn(async () => ({ paths: [] })),
  getSharedBlockOptions: vi.fn(async () => ({ options: [] })),
  getProfile: vi.fn(async () => ({
    profile: { briefing_email_html: true, managed: managed.value },
  })),
});

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

const person: User = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: false,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: true,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: false,
  },
};

afterEach(() => cleanup());

describe('Send as HTML', () => {
  it('is locked and badged when the deployment manages it', async () => {
    managed.value = ['briefing_email_html'];
    render(Harness, { component: Page, user: person });
    const toggle = await screen.findByRole('checkbox', { name: /Send as HTML/ });
    await waitFor(() => expect(toggle).toBeDisabled());
    expect(screen.getByText('Set by your administrator')).toBeInTheDocument();
  });

  it('stays editable otherwise', async () => {
    managed.value = [];
    render(Harness, { component: Page, user: person });
    const toggle = await screen.findByRole('checkbox', { name: /Send as HTML/ });
    expect(toggle).not.toBeDisabled();
    expect(screen.queryByText('Set by your administrator')).not.toBeInTheDocument();
  });
});
