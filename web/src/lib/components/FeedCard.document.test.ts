/**
 * Attached documents in a feed card (Are.na Attachment blocks — PDFs).
 *
 * Are.na renders a cover page for an uploaded PDF, so these arrive looking
 * exactly like image posts: one image, a title, a body. That made the card's
 * hero a lightbox trigger, and clicking a 19 MB essay zoomed page 1 as a
 * picture with no way through to the document — the same wrong-affordance
 * bug embeds had, wearing a different hat.
 *
 * The contract: for an entry carrying `file_url` the hero is a real link to
 * the file, badged with its format, and the lightbox stays out of the way.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import type { FeedEntry } from '$lib/api';
import FeedCard from './FeedCard.svelte';

afterEach(cleanup);

function entry(over: Partial<FeedEntry> = {}): FeedEntry {
  return {
    id: 1,
    title: 'The Cognitive Style of PowerPoint',
    url: 'https://www.are.na/block/45295848',
    content: '<p><a href="https://attachments.are.na/1/e.pdf">Open PDF (5.7 MB)</a></p>',
    images: ['https://cdn.are.na/45295848/cover.png'],
    duplicate_image_count: 0,
    embed_url: '',
    file_url: 'https://attachments.are.na/1/e.pdf',
    media_url: '',
    media_type: '',
    feed: {
      id: 1,
      title: 'pdf-library',
      site_url: 'https://www.are.na/channel/pdf-library',
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

function mount(e: FeedEntry, onImageClick = () => {}) {
  return render(FeedCard, { props: { entry: e, onImageClick } });
}

describe('a card for an attached document', () => {
  it('shows the cover page', () => {
    const { container } = mount(entry());
    expect(container.querySelector('img')?.getAttribute('src')).toBe(
      'https://cdn.are.na/45295848/cover.png',
    );
  });

  it('makes the cover a link to the document, not a lightbox trigger', () => {
    const { container } = mount(entry());

    const hero = container.querySelector('.card-document');
    expect(hero?.tagName).toBe('A');
    expect(hero?.getAttribute('href')).toBe('https://attachments.are.na/1/e.pdf');
  });

  it('does not open the lightbox when the cover is clicked', async () => {
    let lightboxOpened = false;
    const { container } = mount(entry(), () => {
      lightboxOpened = true;
    });

    await fireEvent.click(container.querySelector('.card-document') as HTMLElement);

    expect(lightboxOpened).toBe(false);
  });

  it('opens in a new tab without handing over the opener', () => {
    const { container } = mount(entry());
    const hero = container.querySelector('.card-document')!;
    expect(hero.getAttribute('target')).toBe('_blank');
    expect(hero.getAttribute('rel')).toContain('noopener');
  });

  it('badges the format so a document is distinguishable from a photo', () => {
    const { container } = mount(entry());
    expect(container.querySelector('.doc-badge')?.textContent?.trim()).toBe('PDF');
  });

  it('says what it links to, for a screen reader', () => {
    const { getByLabelText } = mount(entry());
    expect(getByLabelText(/pdf/i)).toBeTruthy();
  });
});

describe('a document with no cover page', () => {
  it('still offers a way through to the file', () => {
    const { container } = mount(entry({ images: [] }));
    const hero = container.querySelector('.card-document') as HTMLAnchorElement;
    expect(hero).toBeTruthy();
    expect(hero.getAttribute('href')).toBe('https://attachments.are.na/1/e.pdf');
  });
});

describe('badge derivation', () => {
  it.each([
    ['https://attachments.are.na/1/a.pdf', 'PDF'],
    ['https://attachments.are.na/1/a.epub', 'EPUB'],
    ['https://attachments.are.na/1/a.mp3', 'MP3'],
    // Query suffixes are how Are.na cache-busts; they must not become the badge.
    ['https://attachments.are.na/1/a.pdf?1776186521', 'PDF'],
    ['https://attachments.are.na/1/noextension', 'FILE'],
  ])('%s → %s', (url, expected) => {
    const { container } = mount(entry({ file_url: url }));
    expect(container.querySelector('.doc-badge')?.textContent?.trim()).toBe(expected);
  });
});

describe('cards that are not documents', () => {
  it('leaves an ordinary image post on the lightbox', async () => {
    let lightboxOpened = false;
    const { container } = mount(entry({ file_url: '' }), () => {
      lightboxOpened = true;
    });

    expect(container.querySelector('.card-document')).toBeNull();
    await fireEvent.click(container.querySelector('button.card-image') as HTMLElement);
    expect(lightboxOpened).toBe(true);
  });

  it('prefers the player when an entry somehow carries both', () => {
    // Shouldn't happen from the provider, but the branches must not both fire.
    const { container } = mount(entry({ embed_url: 'https://youtu.be/abc123XYZ_-' }));
    expect(container.querySelector('.card-video')).toBeTruthy();
    expect(container.querySelector('.card-document')).toBeNull();
  });
});
