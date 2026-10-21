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

const notices = vi.hoisted(() => ({ notifyError: vi.fn(), notifyWarning: vi.fn() }));
vi.mock('@minot/ui/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@minot/ui/stores/notices')>()),
  notifyError: notices.notifyError,
  notifyWarning: notices.notifyWarning,
}));

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
  getEmailAccounts: vi.fn(async () => ({ accounts: [] })),
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
    // The answer arrives long after the click, so it is a notice, not a banner in the card.
    await waitFor(
      () =>
        expect(notices.notifyWarning).toHaveBeenCalledWith(
          expect.stringMatching(/^mk: Nothing was generated/),
        ),
      { timeout: 6000 },
    );
    expect(screen.queryByText(/mk: Nothing was generated/)).toBeNull();
  }, 10_000);

  it('ends the wait when the page goes, with no notice and no further request', async () => {
    answers.queue = [null];
    notices.notifyWarning.mockClear();
    notices.notifyError.mockClear();
    api.runSharedBlock.mockClear();
    page.url = new URL('http://localhost/istota/briefings/settings?view=shared');
    render(Harness, { component: Page, user: admin });
    await pressRunNow();
    await waitFor(() => expect(api.runSharedBlock).toHaveBeenCalledTimes(1));
    const reads = api.getSharedBlocks.mock.calls.length;
    cleanup();
    // The daemon answers after the page has gone; a loop still running would report it.
    answers.queue = [{ id: 'req-2', outcome: 'kept_prior', at: '2026-10-11T00:01:00Z' }];
    await new Promise((resolve) => setTimeout(resolve, 3500));
    expect(api.getSharedBlocks.mock.calls.length).toBe(reads);
    expect(notices.notifyWarning).not.toHaveBeenCalled();
    expect(notices.notifyError).not.toHaveBeenCalled();
  }, 10_000);

  it('reports nothing when the page goes while a poll is in flight', async () => {
    answers.queue = [null];
    notices.notifyWarning.mockClear();
    notices.notifyError.mockClear();
    api.runSharedBlock.mockClear();
    page.url = new URL('http://localhost/istota/briefings/settings?view=shared');
    render(Harness, { component: Page, user: admin });
    await pressRunNow();
    await waitFor(() => expect(api.runSharedBlock).toHaveBeenCalledTimes(1));
    let answerPoll!: (v: unknown) => void;
    api.getSharedBlocks.mockImplementationOnce(
      () => new Promise((resolve) => (answerPoll = resolve)),
    );
    await waitFor(() => expect(answerPoll).toBeTypeOf('function'), { timeout: 3000 });
    cleanup();
    answerPoll({
      shared_blocks: [block({ id: 'req-2', outcome: 'failed', at: '2026-10-11T00:01:00Z' })],
      allowed_source_kinds: ['markets'],
      render_modes: ['synthesis', 'structured'],
      shared_block_timezone: 'UTC',
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(notices.notifyError).not.toHaveBeenCalled();
    expect(notices.notifyWarning).not.toHaveBeenCalled();
  }, 10_000);

  it('shows the last Run now outcome on the block, with no wait running', async () => {
    answers.queue = [{ id: 'req-1', outcome: 'kept_prior', at: '2026-10-11T10:42:00Z' }];
    page.url = new URL('http://localhost/istota/briefings/settings?view=shared');
    render(Harness, { component: Page, user: admin });
    const line = await screen.findByText(
      (_, el) =>
        el?.tagName === 'DIV' && /^Run now: kept prior content,/.test(el.textContent?.trim() ?? ''),
    );
    expect(line.closest('td')).not.toBeNull();
    expect(line.querySelector('time')?.getAttribute('datetime')).toBe('2026-10-11T10:42:00Z');
  });

  it('shows a Run now still waiting for the daemon, then follows it to its outcome', async () => {
    notices.notifyWarning.mockClear();
    api.runSharedBlock.mockClear();
    // Every poll after the first load sees the daemon's answer to the pending request.
    answers.queue = [{ id: 'req-3', outcome: 'kept_prior', at: '2026-10-11T10:51:00Z' }];
    api.getSharedBlocks.mockImplementationOnce(async () => {
      const b = block({ id: 'req-1', outcome: 'failed', at: '2026-10-11T10:42:00Z' });
      b.status.run_pending = { id: 'req-3', name: 'mk', at: '2026-10-11T10:50:00Z' };
      return {
        shared_blocks: [b],
        allowed_source_kinds: ['markets'],
        render_modes: ['synthesis', 'structured'],
        shared_block_timezone: 'UTC',
      };
    });
    page.url = new URL('http://localhost/istota/briefings/settings?view=shared');
    render(Harness, { component: Page, user: admin });
    const waiting = await screen.findByText(/Run now: waiting for the daemon/);
    // One Run now line per block: the pending request, not the older answer beside it.
    expect(waiting.closest('td')?.querySelectorAll('.sb-run')).toHaveLength(1);
    await waitFor(
      () =>
        expect(
          screen.getByText(
            (_, el) =>
              el?.tagName === 'DIV' &&
              /^Run now: kept prior content,/.test(el.textContent?.trim() ?? ''),
          ),
        ).toBeTruthy(),
      { timeout: 4000 },
    );
    expect(screen.queryByText(/Run now: waiting for the daemon/)).toBeNull();
    expect(notices.notifyWarning).toHaveBeenCalledWith(
      expect.stringMatching(/^mk: Nothing was generated/),
    );
    expect(api.runSharedBlock).not.toHaveBeenCalled();
  }, 10_000);

  it('reports nothing when the page goes while a delete is in flight', async () => {
    answers.queue = [null];
    notices.notifyError.mockClear();
    let failDelete!: (e: Error) => void;
    api.deleteSharedBlock.mockImplementationOnce(
      () => new Promise((_, reject) => (failDelete = reject)),
    );
    page.url = new URL('http://localhost/istota/briefings/settings?view=shared');
    render(Harness, { component: Page, user: admin });
    const trigger = await screen.findByLabelText('Shared block actions');
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    await fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(failDelete).toBeTypeOf('function'));
    cleanup();
    failDelete(new Error('Delete failed upstream'));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(notices.notifyError).not.toHaveBeenCalled();
  });
});
