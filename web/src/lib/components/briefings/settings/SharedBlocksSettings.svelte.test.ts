/**
 * Agent-mode shared blocks: the editor takes a task prompt and no sources or
 * trust flag, and the block's row shows its agent task's state.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { SharedBlock, SharedBlockAgentRun } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

function block(over: Partial<SharedBlock>, agent: SharedBlockAgentRun | null = null): SharedBlock {
  return {
    name: 'news',
    cron: '50 5,17 * * *',
    title: 'Headlines',
    directive: 'Read the front pages and write a linked digest.',
    render_mode: 'agent',
    enabled: true,
    trusted: false,
    sources: [],
    created_at: null,
    updated_at: null,
    status: {
      run_pending: null,
      run_answered: null,
      agent,
      last_run_at: null,
      value_updated_at: null,
      value_preview: null,
      stored_trusted: null,
      has_content: false,
    },
    ...over,
  };
}

let listed: SharedBlock[] = [];

await fillApiDouble(api, {
  getSharedBlocks: vi.fn(async () => ({
    shared_blocks: listed,
    allowed_source_kinds: ['browse', 'markets'],
    render_modes: ['synthesis', 'structured', 'agent'],
    shared_block_timezone: 'America/Los_Angeles',
  })),
  putSharedBlock: vi.fn(async () => ({ status: 'ok' })),
});

import SharedBlocksSettings from './SharedBlocksSettings.svelte';

afterEach(() => cleanup());

async function openMenuItem(name: string) {
  const trigger = await screen.findByLabelText('Shared block actions');
  await fireEvent.keyDown(trigger, { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name }));
}

async function pick(trigger: HTMLElement, option: string) {
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const choice = await screen.findByRole('option', { name: option });
  await fireEvent.pointerUp(choice, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(choice);
}

describe('SharedBlocksSettings agent mode', () => {
  it('edits an agent block as a task prompt, with no sources or trust flag', async () => {
    listed = [block({})];
    render(SharedBlocksSettings, { browseOptions: [] });
    await openMenuItem('Edit');
    const prompt = (await screen.findByRole('textbox', {
      name: /Task prompt/,
    })) as HTMLTextAreaElement;
    expect(prompt.rows).toBe(16);
    expect(prompt.value).toBe('Read the front pages and write a linked digest.');
    expect(screen.queryByRole('checkbox', { name: /Trusted/ })).toBeNull();
    expect(screen.queryByRole('button', { name: '+ Add source' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: /Directive/ })).toBeNull();

    await fireEvent.input(prompt, { target: { value: 'A new prompt.' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save shared block' }));
    await waitFor(() =>
      expect(api.putSharedBlock).toHaveBeenCalledWith(
        expect.objectContaining({
          name: 'news',
          render_mode: 'agent',
          directive: 'A new prompt.',
          trusted: false,
          sources: [],
        }),
      ),
    );
  });

  it('offers agent from the listing, and drops sources and trust when switched to it', async () => {
    api.putSharedBlock.mockClear();
    listed = [
      block({
        render_mode: 'synthesis',
        trusted: true,
        sources: [{ kind: 'markets', config: { indices: ['^EXAMPLE'] } }],
      }),
    ];
    render(SharedBlocksSettings, { browseOptions: [] });
    await openMenuItem('Edit');
    expect(await screen.findByRole('checkbox', { name: /Trusted/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: '+ Add source' })).toBeTruthy();

    await pick(screen.getByLabelText('Render mode'), 'Agent (research task)');
    expect(await screen.findByRole('textbox', { name: /Task prompt/ })).toBeTruthy();
    expect(screen.queryByRole('checkbox', { name: /Trusted/ })).toBeNull();
    expect(screen.queryByRole('button', { name: '+ Add source' })).toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: 'Save shared block' }));
    await waitFor(() =>
      expect(api.putSharedBlock).toHaveBeenCalledWith(
        expect.objectContaining({ render_mode: 'agent', trusted: false, sources: [] }),
      ),
    );
  });

  it('shows a queued agent task by its id', async () => {
    listed = [
      block(
        {},
        {
          task_id: 41,
          briefing_name: 'shared_block:news:20261027T125000Z',
          queued_at: '2026-10-27T12:50:00Z',
          captured: false,
          consecutive_failures: 0,
        },
      ),
    ];
    render(SharedBlocksSettings, { browseOptions: [] });
    expect(await screen.findByText(/Agent: queued, task #41/)).toBeTruthy();
  });

  it('shows the captured outcome with its time and the failure run', async () => {
    listed = [
      block(
        {},
        {
          task_id: 42,
          briefing_name: 'shared_block:news:20261027T125000Z',
          queued_at: '2026-10-27T12:50:00Z',
          captured: true,
          outcome: 'failed',
          answered_at: '2026-10-27T13:05:00Z',
          consecutive_failures: 2,
        },
      ),
    ];
    render(SharedBlocksSettings, { browseOptions: [] });
    const line = await screen.findByText(
      (_, el) =>
        el?.tagName === 'DIV' &&
        /^Agent: failed, kept prior content,/.test(el.textContent?.trim() ?? ''),
    );
    expect(line.querySelector('time')?.getAttribute('datetime')).toBe('2026-10-27T13:05:00Z');
    expect(line.textContent).toMatch(/\(2 in a row\)/);
    expect(line.querySelector('.sb-run-failed')).not.toBeNull();
  });
});
