/**
 * Playable media and attached documents in the reader popup.
 *
 * FeedCard learned to tell a video and a PDF apart from a photo; the reader
 * did not, so clicking through to a YouTube block gave you its thumbnail as a
 * lightbox trigger and no player, and a PDF's cover page still zoomed as a
 * picture — the dead end the card fix removed, one click further in.
 *
 * The contract mirrors the card's: an embed's hero plays in place, an
 * attachment's hero is a real link to the file, and neither reaches the
 * lightbox. An ordinary image post is untouched.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import type { FeedEntry } from '$lib/api';
import FeedReader from './FeedReader.svelte';

afterEach(cleanup);

function entry(over: Partial<FeedEntry> = {}): FeedEntry {
  return {
    id: 1,
    title: 'The Working Sheepdog',
    url: 'https://www.are.na/block/76969',
    content: '<p>Border Collies in training</p>',
    images: ['https://cdn.are.na/76969/thumb.jpg'],
    duplicate_image_count: 0,
    embed_url: '',
    file_url: '',
    feed: {
      id: 1,
      title: 'arena-influences',
      site_url: 'https://www.are.na/channel/arena-influences',
      category: null as never,
    },
    status: 'read',
    starred: false,
    starred_at: '',
    published_at: '2026-07-26T10:00:00Z',
    created_at: '2026-07-26T10:00:00Z',
    ...over,
  };
}

function mount(e: FeedEntry, onImageClick = () => {}) {
  return render(FeedReader, {
    props: { entries: [e], index: 0, onClose: () => {}, onImageClick },
  });
}

const video = () => entry({ embed_url: 'https://www.youtube.com/watch?v=B0sO1wdBhMY' });
const doc = () =>
  entry({
    title: 'The Cognitive Style of PowerPoint',
    file_url: 'https://attachments.are.na/1/e.pdf',
    images: ['https://cdn.are.na/45295848/cover.png'],
  });

describe('the reader on playable media', () => {
  it('offers a play control over the thumbnail', () => {
    const { getByLabelText } = mount(video());
    expect(getByLabelText(/play/i)).toBeTruthy();
  });

  it('names the provider so the control says where it plays', () => {
    const { getByLabelText } = mount(video());
    expect(getByLabelText(/youtube/i)).toBeTruthy();
  });

  it('does not open the lightbox when the hero is clicked', async () => {
    let opened = false;
    const { getByLabelText } = mount(video(), () => {
      opened = true;
    });
    await fireEvent.click(getByLabelText(/play/i));
    expect(opened).toBe(false);
  });

  it('swaps in a player on click, sandboxed and pointed at the no-cookie host', async () => {
    const { container, getByLabelText } = mount(video());
    expect(container.querySelector('iframe')).toBeNull();

    await fireEvent.click(getByLabelText(/play/i));

    const frame = container.querySelector('iframe') as HTMLIFrameElement;
    expect(frame).toBeTruthy();
    expect(frame.getAttribute('src')).toBe(
      'https://www.youtube-nocookie.com/embed/B0sO1wdBhMY?autoplay=1',
    );
    const sandbox = frame.getAttribute('sandbox') ?? '';
    expect(sandbox).not.toContain('allow-top-navigation');
  });

  it('falls back to the ordinary image hero for a provider it cannot vouch for', () => {
    const { container } = mount(entry({ embed_url: 'https://evil.test/watch?v=abc' }));
    expect(container.querySelector('.reader-video')).toBeNull();
    expect(container.querySelector('.hero-img')).toBeTruthy();
  });
});

describe('the reader on an attached document', () => {
  it('makes the cover a link to the file rather than a lightbox trigger', async () => {
    let opened = false;
    const { container } = mount(doc(), () => {
      opened = true;
    });

    const hero = container.querySelector('.reader-document') as HTMLAnchorElement;
    expect(hero).toBeTruthy();
    expect(hero.tagName).toBe('A');
    expect(hero.getAttribute('href')).toBe('https://attachments.are.na/1/e.pdf');
    expect(hero.getAttribute('target')).toBe('_blank');
    expect(hero.getAttribute('rel')).toContain('noopener');

    await fireEvent.click(hero);
    expect(opened).toBe(false);
  });

  it('badges the format', () => {
    const { container } = mount(doc());
    expect(container.querySelector('.doc-badge')?.textContent?.trim()).toBe('PDF');
  });

  it('still shows something for a document with no cover page', () => {
    const { container } = mount(doc() && entry({ file_url: 'https://a.are.na/1.pdf', images: [] }));
    expect(container.querySelector('.reader-document')).toBeTruthy();
  });

  it('prefers the player when an entry somehow carries both', () => {
    const { container } = mount(
      entry({
        embed_url: 'https://www.youtube.com/watch?v=B0sO1wdBhMY',
        file_url: 'https://a.are.na/1.pdf',
      }),
    );
    expect(container.querySelector('.reader-video')).toBeTruthy();
    expect(container.querySelector('.reader-document')).toBeNull();
  });
});

describe('the reader on an ordinary image post', () => {
  it('keeps the lightbox hero', async () => {
    let opened = false;
    const { container } = mount(entry(), () => {
      opened = true;
    });
    expect(container.querySelector('.reader-video')).toBeNull();
    expect(container.querySelector('.reader-document')).toBeNull();

    await fireEvent.click(container.querySelector('.hero-img') as HTMLElement);
    expect(opened).toBe(true);
  });
});
