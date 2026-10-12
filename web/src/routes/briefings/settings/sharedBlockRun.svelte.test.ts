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
    sources: [],
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
  it('waits past a stale answer for its own, and reports what the run came to', async () => {
    answers.queue = [
      { id: 'req-1', outcome: 'regenerated', at: '2026-10-11T00:00:00Z' },
      { id: 'req-1', outcome: 'regenerated', at: '2026-10-11T00:00:00Z' },
      { id: 'req-2', outcome: 'kept_prior', at: '2026-10-11T00:01:00Z' },
    ];
    render(Harness, { component: Page, user: admin });
    await pressRunNow();
    expect(api.runSharedBlock).toHaveBeenCalledWith('mk');
    await waitFor(() => expect(screen.getByText(/mk: Nothing was generated/)).toBeInTheDocument(), {
      timeout: 6000,
    });
  }, 10_000);
});
