import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';

/**
 * The two entry-retention controls on feeds settings (ISSUE-388).
 *
 * `entry_retention_days` and `max_entries_per_feed` are the only two settings
 * on this page whose effect is deletion, so the assertions that matter are the
 * ones separating three states the wire treats differently and a number input
 * renders identically: unset (the key is absent and the placeholder shows the
 * constant), `0` (a real value meaning the limit is off), and a typed number.
 * A control that sent `0` for a blank field would switch pruning off for
 * everyone who never touched it; one that dropped a typed `0` would turn a
 * deliberate "off" back into the 90-day default.
 *
 * The save payload is asserted through the module header's save contributor
 * rather than a button in this component, for the reason `addCategory` states:
 * a settings page has exactly one Save and it lives in the app bar.
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

import {
  getFeedsConfig,
  getModuleServices,
  putFeedsConfig,
  type FeedsConfigSettings,
} from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';
import Page from './+page.svelte';

afterEach(cleanup);

const AGE_LABEL = 'Keep read entries for (days)';
const MAX_LABEL = 'Maximum stored entries per feed';

const AGE_HINT =
  'Counted from when an entry was added to your reader, not from when it was ' +
  'published. Unread and starred entries are always kept, and so is anything ' +
  'the feed still returns. At least 50 entries per feed are kept whatever ' +
  'their age, or the maximum below if you set it lower. 0 turns age pruning off; blank uses the default (90). Older ' +
  'entries disappear from the reader.';

const MAX_HINT =
  'The most recently added entries are kept. Starred entries are always kept ' +
  'and can put a feed above this limit. 0 turns the limit off; blank uses the ' +
  'default (5000). Raising it lets the next full fetch store more.';

/** The page's own settings type, so a key it cannot carry fails the check. */
type Settings = FeedsConfigSettings;

function mockConfig(settings: Settings) {
  vi.mocked(getFeedsConfig).mockResolvedValue({
    config: { settings, categories: [], feeds: [] },
    diagnostics: null,
    feed_state: [],
  } as never);
}

beforeEach(() => {
  // The payload assertions read `mock.calls[0]`, and mock calls persist across
  // tests by default — so without this each one reads the first save of the
  // whole file and passes on somebody else's payload.
  vi.clearAllMocks();
  vi.mocked(getModuleServices).mockResolvedValue({ module_enabled: true, services: [] } as never);
  vi.mocked(putFeedsConfig).mockResolvedValue({
    sync: { feeds_added: 0, feeds_updated: 0, categories_added: 0 },
  } as never);
  mockConfig({});
});

/** Mount the page and wait out its onMount load. */
async function mountPage(settings: Settings = {}) {
  mockConfig(settings);
  render(Page);
  await screen.findByRole('button', { name: '+ Add category' });
}

/**
 * The input under a settings field, found by the field's caption.
 *
 * `Field` renders the caption and the control inside one `<label>`, and the
 * caption span also holds the hint popover's "?" trigger — so the caption's
 * text content carries a trailing "?" that the label copy does not.
 */
function fieldInput(label: string): HTMLInputElement {
  const caption = (l: Element) =>
    l.querySelector('.field-label')?.textContent?.trim().replace(/\?$/, '').trim();
  const field = [...document.querySelectorAll('label.field')].find((l) => caption(l) === label);
  if (!field) throw new Error(`no field captioned "${label}"`);
  const input = field.querySelector('input');
  if (!input) throw new Error(`field "${label}" has no input`);
  return input as HTMLInputElement;
}

/** Drive the page's save the way the app-bar button does. */
async function saveViaHeader() {
  const aggregate = get(settingsSave);
  if (!aggregate) throw new Error('the page registered no save contributor');
  await aggregate.save();
}

/** The settings object the page sent to the API. */
function sentSettings(): Settings {
  const calls = vi.mocked(putFeedsConfig).mock.calls;
  if (calls.length === 0) throw new Error('the page saved nothing');
  return (calls[0][0] as { settings: Settings }).settings;
}

describe('entry retention settings', () => {
  describe('loading', () => {
    it('shows stored values in both inputs', async () => {
      await mountPage({ entry_retention_days: 30, max_entries_per_feed: 1200 });

      expect(fieldInput(AGE_LABEL)).toHaveValue(30);
      expect(fieldInput(MAX_LABEL)).toHaveValue(1200);
    });

    it('leaves both inputs blank with the defaults as placeholders when unset', async () => {
      // Unset is not the same as the default: the server omits the key, and
      // filling the box with 90 would make the next save store a number the
      // user never chose.
      await mountPage({});

      expect(fieldInput(AGE_LABEL)).toHaveValue(null);
      expect(fieldInput(AGE_LABEL)).toHaveAttribute('placeholder', '90');
      expect(fieldInput(MAX_LABEL)).toHaveValue(null);
      expect(fieldInput(MAX_LABEL)).toHaveAttribute('placeholder', '5000');
    });

    it('shows a stored zero rather than rendering it as blank', async () => {
      // `0` means the limit is off. Rendered blank it reads as the default,
      // which is the opposite instruction.
      await mountPage({ entry_retention_days: 0, max_entries_per_feed: 0 });

      expect(fieldInput(AGE_LABEL)).toHaveValue(0);
      expect(fieldInput(MAX_LABEL)).toHaveValue(0);
    });

    it('refuses negative input at the control', async () => {
      await mountPage({});

      expect(fieldInput(AGE_LABEL)).toHaveAttribute('min', '0');
      expect(fieldInput(MAX_LABEL)).toHaveAttribute('min', '0');
    });
  });

  describe('saving', () => {
    it('sends both typed values', async () => {
      await mountPage({});

      await fireEvent.input(fieldInput(AGE_LABEL), { target: { value: '45' } });
      await fireEvent.input(fieldInput(MAX_LABEL), { target: { value: '250' } });
      await saveViaHeader();

      expect(sentSettings()).toMatchObject({
        entry_retention_days: 45,
        max_entries_per_feed: 250,
      });
    });

    it('sends numbers rather than the input strings', async () => {
      // The route rejects a string with a 400, so a payload carrying "45"
      // would fail the save outright.
      await mountPage({});

      await fireEvent.input(fieldInput(AGE_LABEL), { target: { value: '45' } });
      await saveViaHeader();

      expect(sentSettings().entry_retention_days).toBe(45);
    });

    it('sends a typed zero as a value', async () => {
      await mountPage({ entry_retention_days: 90, max_entries_per_feed: 5000 });

      await fireEvent.input(fieldInput(AGE_LABEL), { target: { value: '0' } });
      await fireEvent.input(fieldInput(MAX_LABEL), { target: { value: '0' } });
      await saveViaHeader();

      expect(sentSettings()).toMatchObject({
        entry_retention_days: 0,
        max_entries_per_feed: 0,
      });
    });

    it('drops the key when a field is cleared', async () => {
      // Clearing is how the page resets a setting to its default: the wire has
      // no separate reset verb, and the route treats an absent key as "delete
      // the stored row". Sending `0` here would switch pruning off instead.
      await mountPage({ entry_retention_days: 30, max_entries_per_feed: 1200 });

      await fireEvent.input(fieldInput(AGE_LABEL), { target: { value: '' } });
      await fireEvent.input(fieldInput(MAX_LABEL), { target: { value: '' } });
      await saveViaHeader();

      expect(sentSettings()).not.toHaveProperty('entry_retention_days');
      expect(sentSettings()).not.toHaveProperty('max_entries_per_feed');
    });

    it('leaves the other feeds settings alone', async () => {
      // One settings object, one wholesale save — a retention edit that
      // dropped the image window would silently reset it.
      await mountPage({ default_poll_interval_minutes: 45, image_dedupe_window_days: 21 });

      await fireEvent.input(fieldInput(MAX_LABEL), { target: { value: '250' } });
      await saveViaHeader();

      expect(sentSettings()).toEqual({
        default_poll_interval_minutes: 45,
        image_dedupe_window_days: 21,
        max_entries_per_feed: 250,
      });
    });
  });

  describe('policy hints', () => {
    // These two fields are the only place a user is told what deletion does,
    // so the copy is asserted in full rather than by keyword. Each hint sits
    // behind a popover trigger, so it is not on the page until asked for.
    it('explains the age window behind its trigger', async () => {
      await mountPage({});

      expect(screen.queryByText(AGE_HINT)).not.toBeInTheDocument();
      await fireEvent.click(screen.getByLabelText(`About ${AGE_LABEL}`));

      expect(await screen.findByText(AGE_HINT)).toBeInTheDocument();
    });

    it('explains the maximum behind its trigger', async () => {
      await mountPage({});

      expect(screen.queryByText(MAX_HINT)).not.toBeInTheDocument();
      await fireEvent.click(screen.getByLabelText(`About ${MAX_LABEL}`));

      expect(await screen.findByText(MAX_HINT)).toBeInTheDocument();
    });
  });
});
