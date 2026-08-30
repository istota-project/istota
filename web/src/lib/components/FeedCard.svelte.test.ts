/**
 * Feeds Images / Text chips — *display* toggles, not filters, and desktop-only.
 *
 * Two halves to the contract:
 *
 * 1. FeedCard knows nothing about the toggles. It always renders its media and
 *    its body copy, so no toggle can ever remove an entry from the feed.
 * 2. The hiding is CSS on the grid, scoped to a desktop media query — which is
 *    what makes the feature desktop-only without any viewport detection in JS.
 *    On a phone the rules don't apply and the chips are hidden.
 *
 * The second half is asserted against the source because the rules are the
 * behaviour here; a jsdom render can't evaluate a media query.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { FeedEntry } from '$lib/api';
import { showImages, showText } from '$lib/stores/feeds';
import FeedCard from './FeedCard.svelte';

afterEach(cleanup);

beforeEach(() => {
  showImages.set(true);
  showText.set(true);
});

function entry(over: Partial<FeedEntry> = {}): FeedEntry {
  return {
    id: 1,
    title: 'A post title',
    url: 'https://example.com/post',
    content: '<p>Body copy of the post.</p>',
    images: ['https://example.com/a.jpg'],
    duplicate_image_count: 0,
    embed_url: '',
    file_url: '',
    media_url: '',
    media_type: '',
    feed: {
      id: 1,
      title: 'Example Feed',
      site_url: 'https://example.com',
      category: null as never,
    },
    status: 'read', // avoids the IntersectionObserver path in trackView
    starred: false,
    starred_at: '',
    published_at: '2026-07-26T10:00:00Z',
    created_at: '2026-07-26T10:00:00Z',
    ...over,
  };
}

function mount(e: FeedEntry) {
  return render(FeedCard, { props: { entry: e, onImageClick: () => {} } });
}

describe('FeedCard is independent of the display toggles', () => {
  it('renders media and body with both toggles on', () => {
    const { container } = mount(entry());

    expect(container.querySelector('.card-image img')).toBeTruthy();
    expect(container.querySelector('.excerpt')).toBeTruthy();
  });

  // The markup must not change with the stores, or the CSS-only mechanism —
  // and with it the desktop-only scoping — silently stops working.
  it('still renders media and body with both toggles off', () => {
    showImages.set(false);
    showText.set(false);
    const { container, getByText } = mount(entry());

    expect(container.querySelector('article.card')).toBeTruthy();
    expect(container.querySelector('.card-image img')).toBeTruthy();
    expect(container.querySelector('.excerpt')).toBeTruthy();
    expect(getByText('A post title')).toBeTruthy();
  });

  it('renders a text entry the same either way', () => {
    showText.set(false);
    const { container } = mount(entry({ images: [] }));

    expect(container.querySelector('.card-body h3')).toBeTruthy();
    expect(container.querySelector('.excerpt')).toBeTruthy();
  });
});

/** Extract the balanced body of the first block opened by `opener`. */
function blockAfter(css: string, opener: string): string {
  const start = css.indexOf(opener);
  if (start < 0) return '';
  let depth = 0;
  for (let i = start + opener.length - 1; i < css.length; i++) {
    if (css[i] === '{') depth++;
    else if (css[i] === '}') {
      depth--;
      if (depth === 0) return css.slice(start, i + 1);
    }
  }
  return '';
}

describe('display-toggle CSS contract', () => {
  const page = readFileSync(resolve(__dirname, '../../routes/feeds/+page.svelte'), 'utf8');
  const layout = readFileSync(resolve(__dirname, '../../routes/feeds/+layout.svelte'), 'utf8');
  const desktopOnly = blockAfter(page, '@media (min-width: 769px) {');

  it('drives the toggles from classes on the grid', () => {
    expect(page).toMatch(/class:hide-images=\{!\$showImages\}/);
    expect(page).toMatch(/class:hide-text=\{!\$showText\}/);
  });

  it('never filters entries out of the list', () => {
    expect(page).toMatch(/\{#each entries as entry/);
    expect(page).not.toMatch(/filteredEntries/);
  });

  it.each([
    ['the media block', '.feed-grid.hide-images :global(.card-image)'],
    ['galleries', '.feed-grid.hide-images :global(.card-gallery)'],
    ['images embedded in body copy', '.feed-grid.hide-images :global(.excerpt img)'],
    ['inline video in body copy', '.feed-grid.hide-images :global(.excerpt video)'],
    ['a video attachment', '.feed-grid.hide-images :global(.card-media:not(.audio))'],
    ['the body copy itself', '.feed-grid.hide-text :global(.excerpt)'],
  ])('hides %s only on desktop', (_label, selector) => {
    expect(desktopOnly).toContain(selector);
  });

  it('leaves an audio attachment drawn, since it is not a picture', () => {
    // The chip means "pictures", and an <audio> player shows none. Hiding it
    // would take the whole entry away rather than de-illustrating it.
    expect(desktopOnly).not.toMatch(/hide-images :global\(\.card-media\)/);
  });

  it('has no hide rule outside the desktop query', () => {
    const outside = page.replace(desktopOnly, '');
    expect(outside).not.toMatch(/\.feed-grid\.hide-(images|text)/);
  });

  it('hides the chips below the same breakpoint', () => {
    const mobile = blockAfter(layout, '@media (max-width: 768px) {');
    expect(mobile).toContain('.filter-group');
    expect(mobile).toMatch(/\.filter-group\s*\{[^}]*display:\s*none/);
  });
});
