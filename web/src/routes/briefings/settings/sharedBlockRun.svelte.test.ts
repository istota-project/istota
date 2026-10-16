/**
 * Shared-block Run now is answered by the daemon (ISSUE-700): the page posts a
 * request, then reloads the listing until `status.run_answered` names it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { SharedBlock, SharedBlockRunAnswer, User } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

const answers = vi.hoisted(() => ({ queue: [] as (SharedBlockRunAnswer | null)[] }));

function block(answer: SharedBlockRunAnswer | null): SharedBlock {
  return {
    name: 'mk',
    cron: '0 6 * * *',
    title: 'Markets',
    directive: '',
    render_mode: 'structured',
    enabled: true,
    trusted: true,
    sources: [{ kind: 'markets', config: { indices: ['^EXAMPLE'] } }],
    created_at: null,
    updated_at: null,
    status: {
      run_pending: null,
      run_answered: answer,
      last_run_at: null,
      value_updated_at: null,
      value_preview: null,
      stored_trusted: null,
      has_content: false,
    },
  } as unknown as SharedBlock;
}

await fillApiDouble(api, {
  getBriefingTargets: vi.fn(async () => ({
    targets: [],
    channels: [],
    timezone: 'UTC',
    source_scopes: {},
  })),
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
  getProfile: vi.fn(async () => ({ profile: { briefing_email_html: true, managed: [] } })),
  getSharedBlocks: vi.fn(async () => ({
    shared_blocks: [block(answers.queue.length > 1 ? answers.queue.shift()! : answers.queue[0])],
    allowed_source_kinds: ['markets'],
    render_modes: ['synthesis', 'structured'],
    shared_block_timezone: 'UTC',
  })),
  runSharedBlock: vi.fn(async () => ({ id: 'req-2' })),
});

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import Page from './+page.svelte';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

const admin: User = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: true,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: true,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: true,
  },
};

afterEach(() => cleanup());

async function pressRunNow() {
  const trigger = await screen.findByLabelText('Shared block actions');
  await fireEvent.keyDown(trigger, { key: 'Enter' });
  await fireEvent.click(await screen.findByText('Run now'));
}

describe('Shared block Run now', () => {
  it('keeps the shared source editor and admin save working', async () => {
    answers.queue = [null];
    api.putSharedBlock.mockResolvedValueOnce({ status: 'ok' });
    page.url = new URL('http://localhost/istota/briefings/settings?view=shared');
    render(Harness, { component: Page, user: admin });
    const trigger = await screen.findByLabelText('Shared block actions');
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    await fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }));
    const indices = await screen.findByRole('textbox', { name: /Indices/ });
    await fireEvent.input(indices, { target: { value: '^SAMPLE' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save shared block' }));
    await waitFor(() =>
      expect(api.putSharedBlock).toHaveBeenCalledWith(
        expect.objectContaining({
          name: 'mk',
          render_mode: 'structured',
          sources: [{ kind: 'markets', config: { indices: ['^SAMPLE'] } }],
        }),
      ),
    );
  });

  it('waits past a stale answer for its own, and reports what the run came to', async () => {
    answers.queue = [
      { id: 'req-1', outcome: 'regenerated', at: '2026-10-11T00:00:00Z' },
      { id: 'req-1', outcome: 'regenerated', at: '2026-10-11T00:00:00Z' },
      { id: 'req-2', outcome: 'kept_prior', at: '2026-10-11T00:01:00Z' },
    ];
    page.url = new URL('http://localhost/istota/briefings/settings?view=shared');
    render(Harness, { component: Page, user: admin });
    await pressRunNow();
    expect(api.runSharedBlock).toHaveBeenCalledWith('mk');
    await waitFor(() => expect(screen.getByText(/mk: Nothing was generated/)).toBeInTheDocument(), {
      timeout: 6000,
    });
  }, 10_000);
});
