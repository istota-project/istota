import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { BriefingBlock, BriefingBlockTemplate, User } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

const user = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: false,
  features: { briefings: true, admin: false },
} as User;

const templates: BriefingBlockTemplate[] = [
  {
    id: 'calendar',
    label: 'Calendar',
    description: 'Your connected calendars.',
    block: {
      title: 'Calendar',
      directive: '',
      render_mode: 'structured',
      options: { presentation: { width: 'auto' } },
      sources: [{ kind: 'calendar', config: {}, enabled: true }],
    },
  },
  {
    id: 'feeds',
    label: 'News from feeds',
    description: 'Choose a subscription.',
    block: {
      title: 'News',
      directive: '',
      render_mode: 'synthesis',
      options: {},
      sources: [{ kind: 'rss', config: {} }],
    },
  },
  {
    id: 'custom',
    label: 'Custom',
    description: 'Choose your own sources.',
    block: {
      title: 'Custom',
      directive: '',
      render_mode: 'synthesis',
      options: {},
      sources: [],
    },
  },
];
const blocks: BriefingBlock[] = [
  {
    id: 1,
    briefing_name: 'morning',
    position: 0,
    title: 'Headlines',
    directive: 'Be concise',
    render_mode: 'synthesis',
    options: {
      tone: 'neutral',
      story_count: 4,
      custom: { retained: true },
      presentation: { width: 'wide' },
    },
    sources: [
      {
        id: 11,
        block_id: 1,
        position: 0,
        kind: 'browse',
        config: { url: 'https://example.com/news', extra: true },
        enabled: false,
      },
    ],
  },
  {
    id: 2,
    briefing_name: 'morning',
    position: 1,
    title: 'Tasks',
    directive: '',
    render_mode: 'synthesis',
    options: {},
    sources: [
      {
        id: 12,
        block_id: 2,
        position: 0,
        kind: 'todos',
        config: { path: 'shared/tasks.md' },
        enabled: true,
      },
    ],
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  api.getBriefings.mockResolvedValue({
    briefings: [
      {
        id: 1,
        name: 'morning',
        cron: '0 7 * * *',
        title: '',
        output: 'email',
        enabled: true,
        managed: 'db',
      },
      {
        id: 2,
        name: 'muted',
        cron: '0 18 * * *',
        title: '',
        output: 'web',
        enabled: false,
        managed: 'db',
      },
    ],
    outputs: ['email', 'web'],
    rooms: [],
  });
  api.getBriefingConfig.mockResolvedValue({
    briefings: [{ name: 'morning', blocks: structuredClone(blocks) }],
    schedule_names: ['morning'],
    source_kinds: ['browse', 'calendar', 'rss', 'todos'],
    structured_kinds: [],
    block_templates: templates,
  });
  api.getBrowsePresets.mockResolvedValue({ presets: [] });
  api.getFeedOptions.mockResolvedValue({
    available: true,
    subscriptions: [{ kind: 'subscription', value: 1, label: 'Example' }],
    categories: [],
  });
  api.getSharedBlockOptions.mockResolvedValue({ options: [] });
  api.getBriefingPathSuggestions.mockResolvedValue({ paths: ['shared/tasks.md'] });
  api.checkBriefingPath.mockResolvedValue({ ok: false });
  api.getProfile.mockResolvedValue({ profile: { briefing_email_html: true, managed: [] } });
  api.putBriefingBlock.mockResolvedValue({
    status: 'ok',
    block: { ...structuredClone(blocks[0]), id: 3 },
  });
  api.deleteBriefingBlock.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function mount() {
  render(Harness, { component: Page, user });
  await screen.findByRole('button', { name: 'Add block' });
}
async function menu(title: string, action: string) {
  await fireEvent.keyDown(screen.getByLabelText(`Actions for ${title}`), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: action }));
}
async function edit(title = 'Headlines') {
  await menu(title, 'Edit');
  return screen.findByRole('dialog', { name: 'Edit block' });
}

describe('briefing block builder', () => {
  it('starts with a searchable server preset and saves all fields in one request', async () => {
    await mount();
    await fireEvent.click(screen.getByRole('button', { name: 'Add block' }));
    const picker = await screen.findByRole('dialog', { name: 'Add block' });
    await fireEvent.input(within(picker).getByRole('searchbox', { name: 'Search presets' }), {
      target: { value: 'calendar' },
    });
    expect(within(picker).queryByRole('button', { name: 'Custom' })).toBeNull();
    await fireEvent.click(within(picker).getByRole('button', { name: 'Calendar' }));
    const editor = await screen.findByRole('dialog', { name: 'New block' });
    expect(api.putBriefingBlock).not.toHaveBeenCalled();
    await fireEvent.click(within(editor).getByRole('radio', { name: /Full width/ }));
    await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
    await waitFor(() => expect(api.putBriefingBlock).toHaveBeenCalledTimes(1));
    expect(api.putBriefingBlock).toHaveBeenCalledWith({
      briefing_name: 'morning',
      title: 'Calendar',
      directive: '',
      render_mode: 'structured',
      options: { presentation: { width: 'full' } },
      sources: [{ kind: 'calendar', config: {}, enabled: true }],
    });
    expect(api.putBriefingSource).not.toHaveBeenCalled();
  });

  it('keeps edits local, confirms cancellation, and retains arbitrary options', async () => {
    await mount();
    let editor = await edit();
    await fireEvent.input(within(editor).getByRole('textbox', { name: 'Title' }), {
      target: { value: 'Edited headlines' },
    });
    await fireEvent.input(within(editor).getByRole('textbox', { name: 'URL' }), {
      target: { value: 'https://example.com/new' },
    });
    await fireEvent.click(within(editor).getByRole('radio', { name: /Full width/ }));
    expect(api.putBriefingBlock).not.toHaveBeenCalled();
    await fireEvent.click(within(editor).getByRole('button', { name: 'Cancel' }));
    const confirm = await screen.findByRole('dialog', { name: 'Discard block changes' });
    await fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }));
    editor = screen.getByRole('dialog', { name: 'Edit block' });
    expect(within(editor).getByRole('textbox', { name: 'Title' })).toHaveValue('Edited headlines');
    await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
    await waitFor(() => expect(api.putBriefingBlock).toHaveBeenCalledTimes(1));
    expect(api.putBriefingBlock).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 1,
        title: 'Edited headlines',
        options: {
          tone: 'neutral',
          story_count: 4,
          custom: { retained: true },
          presentation: { width: 'full' },
        },
        sources: [
          {
            id: 11,
            kind: 'browse',
            config: { url: 'https://example.com/new', extra: true },
            enabled: false,
          },
        ],
      }),
    );
  });

  it('discards a dirty draft without saving on Escape', async () => {
    await mount();
    const editor = await edit();
    await fireEvent.input(within(editor).getByRole('textbox', { name: 'Title' }), {
      target: { value: 'Discard me' },
    });
    await fireEvent.keyDown(editor, { key: 'Escape' });
    const confirm = await screen.findByRole('dialog', { name: 'Discard block changes' });
    await fireEvent.click(within(confirm).getByRole('button', { name: 'Discard changes' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.putBriefingBlock).not.toHaveBeenCalled();
    const reopened = await edit();
    expect(within(reopened).getByRole('textbox', { name: 'Title' })).toHaveValue('Headlines');
  });

  it('keeps the draft and saved list when an atomic save fails', async () => {
    api.putBriefingBlock.mockRejectedValueOnce(new Error('Could not save block'));
    await mount();
    const editor = await edit();
    await fireEvent.input(within(editor).getByRole('textbox', { name: 'Title' }), {
      target: { value: 'Keep this draft' },
    });
    await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
    expect(await within(editor).findByRole('alert')).toHaveTextContent('Could not save block');
    expect(within(editor).getByRole('textbox', { name: 'Title' })).toHaveValue('Keep this draft');
    expect(screen.getByRole('heading', { name: 'Headlines' })).toBeInTheDocument();
  });

  it('moves by keyboard, disables the boundary, and announces the saved position', async () => {
    await mount();
    const trigger = screen.getByLabelText('Actions for Headlines');
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(await screen.findByRole('menuitem', { name: 'Move up' })).toHaveAttribute(
      'data-disabled',
    );
    const down = screen.getByRole('menuitem', { name: 'Move down' });
    down.focus();
    await fireEvent.keyDown(down, { key: 'Enter' });
    await waitFor(() =>
      expect(api.putBriefingBlock).toHaveBeenCalledWith({
        reorder: { briefing_name: 'morning', ordered_ids: [2, 1] },
      }),
    );
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Headlines moved to position 2 of 2',
    );
    expect(
      within(screen.getByRole('list', { name: 'Briefing blocks' }))
        .getAllByRole('heading')
        .map((h) => h.textContent),
    ).toEqual(['Tasks', 'Headlines']);
  });

  it('keeps saved order after a failed move and permits a retry', async () => {
    api.putBriefingBlock.mockRejectedValueOnce(new Error('Order was not saved'));
    await mount();
    await menu('Headlines', 'Move down');
    expect(await screen.findByRole('alert')).toHaveTextContent('Order was not saved');
    expect(
      within(screen.getByRole('list', { name: 'Briefing blocks' }))
        .getAllByRole('heading')
        .map((h) => h.textContent),
    ).toEqual(['Headlines', 'Tasks']);
    await menu('Headlines', 'Move down');
    await waitFor(() => expect(api.putBriefingBlock).toHaveBeenCalledTimes(2));
  });

  it('duplicates every source in one create request, with no IDs', async () => {
    await mount();
    await menu('Headlines', 'Duplicate');
    await waitFor(() => expect(api.putBriefingBlock).toHaveBeenCalledTimes(1));
    expect(api.putBriefingBlock).toHaveBeenCalledWith({
      briefing_name: 'morning',
      title: 'Headlines (copy)',
      directive: 'Be concise',
      render_mode: 'synthesis',
      options: blocks[0].options,
      sources: [{ kind: 'browse', config: blocks[0].sources[0].config, enabled: false }],
    });
    expect(api.putBriefingSource).not.toHaveBeenCalled();
  });

  it('confirms deletion and explains that existing editions remain available', async () => {
    await mount();
    await menu('Headlines', 'Delete');
    const confirm = await screen.findByRole('dialog', { name: 'Delete block' });
    expect(confirm).toHaveTextContent('Existing editions remain available');
    expect(api.deleteBriefingBlock).not.toHaveBeenCalled();
    await fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(api.deleteBriefingBlock).toHaveBeenCalledWith(1));
    expect(screen.queryByRole('heading', { name: 'Headlines' })).toBeNull();
  });

  it('keeps a failed delete visible in its confirmation dialog', async () => {
    api.deleteBriefingBlock.mockRejectedValueOnce(new Error('Delete was not saved'));
    await mount();
    await menu('Headlines', 'Delete');
    const dialog = await screen.findByRole('dialog', { name: 'Delete block' });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Delete was not saved');
    expect(within(dialog).getByRole('button', { name: 'Delete' })).not.toBeDisabled();
  });

  it('keeps legacy zero-source blocks when duplicating a whole briefing', async () => {
    const legacy = structuredClone(blocks[0]);
    legacy.sources = [];
    api.getBriefingConfig.mockResolvedValue({
      briefings: [{ name: 'morning', blocks: [legacy] }],
      schedule_names: ['morning'],
      source_kinds: ['calendar'],
      structured_kinds: [],
      block_templates: templates,
    });
    await mount();
    await fireEvent.keyDown(screen.getByLabelText('Briefing actions'), { key: 'Enter' });
    await fireEvent.click(await screen.findByRole('menuitem', { name: 'Duplicate' }));
    await waitFor(() =>
      expect(api.putBriefingBlock).toHaveBeenCalledWith({
        briefing_name: 'morning-copy',
        title: legacy.title,
        directive: legacy.directive,
        render_mode: legacy.render_mode,
        options: legacy.options,
      }),
    );
    expect(api.upsertBriefing).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'morning-copy', enabled: false }),
    );
  });

  it('includes empty disabled schedules from the live listing and pairs schedule with content', async () => {
    await mount();
    const trigger = screen.getByRole('button', { name: 'Briefing', exact: true });
    await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(trigger);
    const option = await screen.findByRole('option', { name: 'muted (disabled)' });
    await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(option);
    expect(
      screen.getByText('No blocks yet. Add a block to start this briefing.'),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Selected briefing schedule')).toHaveTextContent('0 18 * * *');
    expect(screen.getByLabelText('Selected briefing schedule')).toHaveTextContent('web');
    await fireEvent.click(screen.getByRole('button', { name: 'Edit schedule' }));
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('muted');
  });

  it('edits synthesis controls without losing source state or other options', async () => {
    await mount();
    const editor = await edit();
    await fireEvent.click(within(editor).getByText('Advanced', { selector: 'summary' }));
    await fireEvent.input(within(editor).getByRole('textbox', { name: /Directive/ }), {
      target: { value: 'Only local news' },
    });
    await fireEvent.input(within(editor).getByRole('spinbutton', { name: 'Story count' }), {
      target: { value: '7' },
    });
    await fireEvent.input(within(editor).getByRole('textbox', { name: 'Tone' }), {
      target: { value: 'brief' },
    });
    await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
    await waitFor(() =>
      expect(api.putBriefingBlock).toHaveBeenCalledWith(
        expect.objectContaining({
          directive: 'Only local news',
          options: { ...blocks[0].options, story_count: 7, tone: 'brief' },
          sources: [
            { id: 11, kind: 'browse', config: blocks[0].sources[0].config, enabled: false },
          ],
        }),
      ),
    );
  });

  it('keeps file checks advisory and edits source configuration inside the block draft', async () => {
    await mount();
    const editor = await edit('Tasks');
    const path = within(editor).getByRole('combobox', { name: 'File path' });
    await fireEvent.input(path, { target: { value: 'shared/future-tasks.md' } });
    await waitFor(() =>
      expect(api.checkBriefingPath).toHaveBeenCalledWith('shared/future-tasks.md'),
    );
    expect(await within(editor).findByText(/No file there yet/)).toBeInTheDocument();
    expect(api.putBriefingBlock).not.toHaveBeenCalled();
    await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
    await waitFor(() =>
      expect(api.putBriefingBlock).toHaveBeenCalledWith(
        expect.objectContaining({
          id: 2,
          sources: [
            { id: 12, kind: 'todos', config: { path: 'shared/future-tasks.md' }, enabled: true },
          ],
        }),
      ),
    );
  });

  it('allows an existing zero-source block and removes sources only on Save block', async () => {
    await mount();
    const editor = await edit();
    await fireEvent.click(within(editor).getByRole('button', { name: 'Remove source 1' }));
    expect(within(editor).getByText('No sources')).toBeInTheDocument();
    expect(api.putBriefingBlock).not.toHaveBeenCalled();
    expect(api.deleteBriefingSource).not.toHaveBeenCalled();
    await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
    await waitFor(() =>
      expect(api.putBriefingBlock).toHaveBeenCalledWith(
        expect.objectContaining({ id: 1, sources: [] }),
      ),
    );
  });

  it('disables repeat saves while a request is pending', async () => {
    let finish!: (result: unknown) => void;
    api.putBriefingBlock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await mount();
    const editor = await edit();
    const save = within(editor).getByRole('button', { name: 'Save block' });
    await fireEvent.click(save);
    expect(save).toBeDisabled();
    expect(within(editor).getByRole('textbox', { name: 'Title' })).toBeDisabled();
    await fireEvent.click(save);
    expect(api.putBriefingBlock).toHaveBeenCalledTimes(1);
    finish({ status: 'ok', block: blocks[0] });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('does not append a partial duplicate when the request fails', async () => {
    api.putBriefingBlock.mockRejectedValueOnce(new Error('Copy was not saved'));
    await mount();
    await menu('Headlines', 'Duplicate');
    expect(await screen.findByRole('alert')).toHaveTextContent('Copy was not saved');
    expect(
      within(screen.getByRole('list', { name: 'Briefing blocks' })).getAllByRole('listitem'),
    ).toHaveLength(2);
    expect(api.putBriefingSource).not.toHaveBeenCalled();
  });

  it('explains an unavailable feed preset and validates a custom draft before saving', async () => {
    api.getFeedOptions.mockResolvedValue({ available: false, subscriptions: [], categories: [] });
    await mount();
    await fireEvent.click(screen.getByRole('button', { name: 'Add block' }));
    const picker = await screen.findByRole('dialog', { name: 'Add block' });
    expect(within(picker).getByRole('button', { name: 'News from feeds' })).toBeDisabled();
    expect(picker).toHaveTextContent('Enable Feeds and subscribe to a feed first');
    await fireEvent.click(within(picker).getByRole('button', { name: 'Custom' }));
    const editor = await screen.findByRole('dialog', { name: 'New block' });
    await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
    expect(await within(editor).findByRole('alert')).toHaveTextContent('Add at least one source');
    expect(api.putBriefingBlock).not.toHaveBeenCalled();
    const addSource = within(editor).getByRole('button', { name: 'Add source' });
    await fireEvent.pointerDown(addSource, { pointerType: 'mouse', button: 0 });
    await fireEvent.pointerUp(addSource, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(addSource);
    const calendar = await screen.findByRole('option', { name: 'calendar' });
    await fireEvent.pointerUp(calendar, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(calendar);
    await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
    await waitFor(() =>
      expect(api.putBriefingBlock).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Custom',
          sources: [{ kind: 'calendar', config: {}, enabled: true }],
        }),
      ),
    );
  });
});
