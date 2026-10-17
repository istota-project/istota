/**
 * Feed-supplied URLs as hrefs (ISSUE-774).
 *
 * An entry's link, its feed's site link and an attached file's URL all come
 * from a third party. A `javascript:` URL in any of them, clicked, runs on this
 * origin with the user's session, so the card and the reader only ever put an
 * http(s) URL in an href. Rows stored before ingestion checked a scheme are the
 * reason this holds on the frontend too.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { FeedEntry } from '$lib/api';
import FeedCard from './FeedCard.svelte';
import FeedReader from './FeedReader.svelte';

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const UNSAFE = [
  'javascript:alert(document.cookie)',
  'JaVaScRiPt:alert(1)',
  '  javascript:alert(1)',
  'java\tscript:alert(1)',
  'data:text/html,<script>alert(1)</script>',
  'vbscript:msgbox(1)',
];

function entry(over: Partial<FeedEntry> = {}, siteUrl = 'https://example.com/'): FeedEntry {
  return {
    id: 1,
    title: 'Click me',
    url: 'https://example.com/post/1',
    content: '<p>body</p>',
    images: [],
    duplicate_image_count: 0,
    embed_url: '',
    file_url: '',
    media_url: '',
    media_type: '',
    feed: { id: 1, title: 'Example', site_url: siteUrl, category: null as never },
    status: 'read',
    starred: false,
    starred_at: '',
    published_at: '2026-07-26T10:00:00Z',
    created_at: '2026-07-26T10:00:00Z',
    ...over,
  };
}

function hrefs(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('[href]')).map((a) => a.getAttribute('href') ?? '');
}

function onlyHttp(container: HTMLElement) {
  for (const href of hrefs(container)) {
    expect(new URL(href, 'https://istota.invalid/').protocol).toMatch(/^https?:$/);
  }
}

const mountCard = (e: FeedEntry) =>
  render(FeedCard, { props: { entry: e, onImageClick: () => {} } }).container;
// The reader is a Modal, which portals its content to <body>.
const mountReader = (e: FeedEntry) => {
  render(FeedReader, { props: { entries: [e], index: 0, onClose: () => {} } });
  return document.body;
};

describe.each([
  ['FeedCard', mountCard],
  ['FeedReader', mountReader],
])('%s with an unsafe link', (_name, mount) => {
  it.each(UNSAFE)('never renders %j as an entry href', (bad) => {
    const container = mount(entry({ url: bad }, bad));
    onlyHttp(container);
    expect(hrefs(container).some((h) => /script|data:/i.test(h))).toBe(false);
  });

  it.each(UNSAFE)('never renders %j as a file href', (bad) => {
    const container = mount(entry({ file_url: bad }));
    onlyHttp(container);
  });

  it("falls back to the feed's site link when only the entry link is unsafe", () => {
    const container = mount(entry({ url: 'javascript:alert(1)' }));
    expect(hrefs(container)).toContain('https://example.com/');
    onlyHttp(container);
  });

  it('keeps an ordinary http(s) entry link', () => {
    const container = mount(entry());
    expect(hrefs(container)).toContain('https://example.com/post/1');
  });
});
