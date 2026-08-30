import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/svelte';

/**
 * "+ Add category" on feeds settings.
 *
 * It collected the slug through a browser `prompt()` — the last native dialog
 * in the web UI, on a page that already opens a `Modal` for add-subscription
 * and edit-subscription and a `ConfirmDialog` for deleting a category. The
 * dialog also cost the interaction two things a modal gets for free: the
 * duplicate-slug check could only report into the page-level banner *after*
 * the dialog had closed and taken the draft with it, and a prompt asks one
 * question, so the title arrived as a copy of the slug and had to be corrected
 * afterwards in the table.
 *
 * The load-bearing assertion is the `window.prompt` spy. A modal that renders
 * while a prompt still fires would satisfy every other check here, and jsdom's
 * own `prompt` throws "not implemented" rather than returning — so a test that
 * only looked for the modal could pass against a page still calling it.
 */

vi.mock('$lib/api', () => ({
  getFeedsConfig: vi.fn(),
  putFeedsConfig: vi.fn(),
  importOpml: vi.fn(),
  exportOpmlUrl: vi.fn(() => '/istota/api/feeds/opml'),
  refreshFeeds: vi.fn(),
  getModuleServices: vi.fn(),
}));

import { get } from 'svelte/store';

import { getFeedsConfig, getModuleServices, putFeedsConfig } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';
import Page from './+page.svelte';

afterEach(cleanup);

const categories = [{ slug: 'blogs', title: 'Blogs' }];

beforeEach(() => {
  vi.mocked(getModuleServices).mockResolvedValue({ module_enabled: true, services: [] } as never);
  vi.mocked(getFeedsConfig).mockResolvedValue({
    config: { settings: {}, categories: structuredClone(categories), feeds: [] },
    diagnostics: null,
    feed_state: [],
  } as never);
});

/** Mount the page and wait out its onMount load. */
async function mountPage() {
  render(Page);
  await screen.findByRole('button', { name: '+ Add category' });
}

async function openAddCategory() {
  await fireEvent.click(screen.getByRole('button', { name: '+ Add category' }));
  return await screen.findByRole('dialog');
}

/**
 * Run the page's save.
 *
 * There is no Save button in this component to click: a settings page has
 * exactly one, it lives in the app bar, and the page contributes it through
 * `useSettingsSave` while `HeaderSave` renders it from the *module* layout
 * (web/AGENTS.md, "A settings page has exactly one Save"). So the store is the
 * seam, and driving it is what a click on that button would do.
 */
async function saveViaHeader() {
  const aggregate = get(settingsSave);
  if (!aggregate) throw new Error('the page registered no save contributor');
  await aggregate.save();
}

/** The slug/title inputs, read off the modal's own labels. */
function categoryFields(dialog: HTMLElement) {
  return {
    slug: within_(dialog, 'Slug'),
    title: within_(dialog, 'Title (optional)'),
  };
}

// `Field` renders the caption and the control inside one `<label>`, so the
// input is a descendant of the element carrying the label text rather than a
// sibling of it.
function within_(dialog: HTMLElement, label: string): HTMLInputElement {
  const field = [...dialog.querySelectorAll('label.field')].find(
    (l) => l.querySelector('.field-label')?.textContent?.trim() === label,
  );
  if (!field) throw new Error(`no field labelled "${label}" in the dialog`);
  const input = field.querySelector('input');
  if (!input) throw new Error(`field "${label}" has no input`);
  return input as HTMLInputElement;
}

describe('add category', () => {
  it('opens the app modal and never calls the native prompt', async () => {
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue(null);
    await mountPage();

    const dialog = await openAddCategory();

    expect(prompt).not.toHaveBeenCalled();
    expect(dialog).toHaveTextContent('Add category');
  });

  it('adds the category with the title as typed, not as a copy of the slug', async () => {
    await mountPage();
    const dialog = await openAddCategory();
    const fields = categoryFields(dialog);

    await fireEvent.input(fields.slug, { target: { value: 'zines' } });
    await fireEvent.input(fields.title, { target: { value: 'Zines and comics' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const row = screen.getByText('zines').closest('tr');
    expect(row).not.toBeNull();
    expect(row!.querySelector('input')).toHaveValue('Zines and comics');
  });

  // ISSUE-346's own sketch asked for a blank title here, on the reading that
  // the table's `placeholder={cat.slug}` made one read correctly. It does in
  // that table and nowhere else: `feed_categories.title` is NOT NULL, and the
  // reader's sidebar renders a falsy title as "uncategorized", so a blank one
  // would file a real category under that heading. The server mirrors a blank
  // title onto the slug for exactly that reason, so mirroring on the client is
  // what makes the table agree with what a reload will show.
  it('mirrors the slug onto a blank title, as the save will', async () => {
    await mountPage();
    const dialog = await openAddCategory();

    await fireEvent.input(categoryFields(dialog).slug, { target: { value: 'zines' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const row = screen.getByText('zines').closest('tr');
    expect(row!.querySelector('input')).toHaveValue('zines');
  });

  // The assertion that survives a reload. Everything above is client state,
  // which cannot see what the server does with a title — and the server has
  // its own rule about a blank one.
  it('sends the typed title to the server rather than the slug', async () => {
    vi.mocked(putFeedsConfig).mockResolvedValue({
      sync: { feeds_added: 0, feeds_updated: 0, categories_added: 1 },
    } as never);
    await mountPage();
    const dialog = await openAddCategory();
    const fields = categoryFields(dialog);

    await fireEvent.input(fields.slug, { target: { value: 'zines' } });
    await fireEvent.input(fields.title, { target: { value: 'Zines and comics' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    await saveViaHeader();

    const sent = vi.mocked(putFeedsConfig).mock.calls[0][0] as {
      categories: { slug: string; title?: string }[];
    };
    expect(sent.categories).toContainEqual({ slug: 'zines', title: 'Zines and comics' });
  });

  it('reports a duplicate slug inside the modal and keeps the draft', async () => {
    await mountPage();
    const dialog = await openAddCategory();
    const fields = categoryFields(dialog);

    await fireEvent.input(fields.slug, { target: { value: 'blogs' } });
    await fireEvent.input(fields.title, { target: { value: 'Second try' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    // Still open, error beside the field, and both fields as typed — the whole
    // point of the change over a prompt, which closed and dropped the draft.
    const stillOpen = screen.getByRole('dialog');
    expect(stillOpen).toHaveTextContent('already exists');
    expect(categoryFields(stillOpen).slug).toHaveValue('blogs');
    expect(categoryFields(stillOpen).title).toHaveValue('Second try');
    expect(screen.getAllByText('blogs')).toHaveLength(1);
  });

  it('discards the draft on cancel', async () => {
    await mountPage();
    const dialog = await openAddCategory();

    await fireEvent.input(categoryFields(dialog).slug, { target: { value: 'zines' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByText('zines')).toBeNull();
  });

  it('refuses a blank slug without closing', async () => {
    await mountPage();
    const dialog = await openAddCategory();

    await fireEvent.input(categoryFields(dialog).title, { target: { value: 'Title only' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    expect(screen.getByRole('dialog')).toHaveTextContent('Slug is required');
  });
});
