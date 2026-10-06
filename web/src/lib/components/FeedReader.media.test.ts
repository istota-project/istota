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
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import type { FeedEntry } from '$lib/api';
import FeedReader from './FeedReader.svelte';
import Lightbox from './Lightbox.svelte';

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

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
    media_url: '',
    media_type: '',
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
    const { getByLabelText } = mount(video());
    expect(document.querySelector('iframe')).toBeNull();

    await fireEvent.click(getByLabelText(/play/i));

    const frame = document.querySelector('iframe') as HTMLIFrameElement;
    expect(frame).toBeTruthy();
    expect(frame.getAttribute('src')).toBe(
      'https://www.youtube-nocookie.com/embed/B0sO1wdBhMY?autoplay=1',
    );
    const sandbox = frame.getAttribute('sandbox') ?? '';
    expect(sandbox).not.toContain('allow-top-navigation');
  });

  it('falls back to the ordinary image hero for a provider it cannot vouch for', () => {
    mount(entry({ embed_url: 'https://evil.test/watch?v=abc' }));
    expect(document.querySelector('.reader-video')).toBeNull();
    expect(document.querySelector('.hero-img')).toBeTruthy();
  });
});

describe('the reader on an attached document', () => {
  it('makes the cover a link to the file rather than a lightbox trigger', async () => {
    let opened = false;
    mount(doc(), () => {
      opened = true;
    });

    const hero = document.querySelector('.reader-document') as HTMLAnchorElement;
    expect(hero).toBeTruthy();
    expect(hero.tagName).toBe('A');
    expect(hero.getAttribute('href')).toBe('https://attachments.are.na/1/e.pdf');
    expect(hero.getAttribute('target')).toBe('_blank');
    expect(hero.getAttribute('rel')).toContain('noopener');

    await fireEvent.click(hero);
    expect(opened).toBe(false);
  });

  it('badges the format', () => {
    mount(doc());
    expect(document.querySelector('.doc-badge')?.textContent?.trim()).toBe('PDF');
  });

  it('still shows something for a document with no cover page', () => {
    mount(doc() && entry({ file_url: 'https://a.are.na/1.pdf', images: [] }));
    expect(document.querySelector('.reader-document')).toBeTruthy();
  });

  it('prefers the player when an entry somehow carries both', () => {
    mount(
      entry({
        embed_url: 'https://www.youtube.com/watch?v=B0sO1wdBhMY',
        file_url: 'https://a.are.na/1.pdf',
      }),
    );
    expect(document.querySelector('.reader-video')).toBeTruthy();
    expect(document.querySelector('.reader-document')).toBeNull();
  });
});

describe('the reader on a direct media attachment (ISSUE-356)', () => {
  function clip(over: Partial<FeedEntry> = {}) {
    return entry({
      title: 'a clip',
      images: [],
      media_url: 'https://assets.example.town/media/117/clip.mp4',
      media_type: 'video/mp4',
      ...over,
    });
  }

  it('renders a <video>, not an <img>', () => {
    mount(clip());
    expect(document.querySelector('video')).toBeTruthy();
    expect(document.querySelector('img[src$=".mp4"]')).toBeNull();
  });

  it('is bounded by CSS alone — no width or height attribute', () => {
    mount(clip());
    const video = document.querySelector('video') as HTMLVideoElement;
    expect(video.hasAttribute('width')).toBe(false);
    expect(video.hasAttribute('height')).toBe(false);
    expect(video.hasAttribute('controls')).toBe(true);
    expect(video.hasAttribute('autoplay')).toBe(false);
  });

  it('does not reach the lightbox', async () => {
    let opened = false;
    mount(clip(), () => {
      opened = true;
    });
    await fireEvent.click(document.querySelector('video') as HTMLElement);
    expect(opened).toBe(false);
  });

  it('uses an accompanying still as the poster', () => {
    mount(clip({ images: ['https://assets.example.town/media/119/still.jpg'] }));
    const video = document.querySelector('video') as HTMLVideoElement;
    expect(video.getAttribute('poster')).toBe('https://assets.example.town/media/119/still.jpg');
    // The still is the poster, not a second hero beside the player.
    expect(document.querySelector('.hero-img')).toBeNull();
  });

  it('draws several stills under the player rather than eating one', () => {
    // The reader is the last place a picture could be recovered, so an entry
    // carrying both a clip and a gallery must not lose the gallery.
    const images = ['https://a.example/1.jpg', 'https://a.example/2.jpg'];
    mount(clip({ images }));
    const video = document.querySelector('video') as HTMLVideoElement;
    expect(video.hasAttribute('poster')).toBe(false);
    expect(document.querySelectorAll('.hero-img img')).toHaveLength(2);
  });

  it('never uses a playable URL as the poster', () => {
    mount(clip({ images: ['https://assets.example.town/media/117/clip.mp4'] }));
    const video = document.querySelector('video') as HTMLVideoElement;
    expect(video.hasAttribute('poster')).toBe(false);
  });

  it('renders an <audio> for a podcast enclosure', () => {
    mount(clip({ media_url: 'https://pod.example.com/12.mp3', media_type: 'audio/mpeg' }));
    expect(document.querySelector('audio')).toBeTruthy();
    expect(document.querySelector('video')).toBeNull();
  });

  it('plays nothing for a URL that is not http(s)', () => {
    mount(clip({ media_url: 'javascript:alert(1)' }));
    expect(document.querySelector('video')).toBeNull();
    expect(document.querySelector('audio')).toBeNull();
  });

  it('prefers a provider player when an entry carries both', () => {
    mount(clip({ embed_url: 'https://www.youtube.com/watch?v=B0sO1wdBhMY' }));
    expect(document.querySelector('.reader-video')).toBeTruthy();
    expect(document.querySelector('video')).toBeNull();
  });
});

describe('the reader on an ordinary image post', () => {
  it('keeps the lightbox hero', async () => {
    let opened = false;
    mount(entry(), () => {
      opened = true;
    });
    expect(document.querySelector('.reader-video')).toBeNull();
    expect(document.querySelector('.reader-document')).toBeNull();

    await fireEvent.click(document.querySelector('.hero-img') as HTMLElement);
    expect(opened).toBe(true);
  });
});

it('Escape in an image viewer keeps the feed reader open underneath', async () => {
  const closeReader = vi.fn();
  const closeImage = vi.fn();
  const images = ['https://example.com/image.png'];
  render(FeedReader, {
    entries: [entry({ images })],
    index: 0,
    onClose: closeReader,
    onImageClick: () => {},
  });
  render(Lightbox, { images, index: 0, onClose: closeImage });
  const close = screen.getByRole('button', { name: 'Close image' });
  close.focus();
  await fireEvent.keyDown(close, { key: 'Escape' });
  expect(closeImage).toHaveBeenCalledTimes(1);
  expect(closeReader).not.toHaveBeenCalled();
});

it('keeps a lone audio artwork image available for zoom', () => {
  mount(entry({ media_url: 'https://example.com/episode.mp3', media_type: 'audio/mpeg' }));
  expect(document.querySelector('audio')).toBeTruthy();
  expect(document.querySelector('.hero-img img')).toHaveAttribute(
    'src',
    'https://cdn.are.na/76969/thumb.jpg',
  );
});

it('releases the player on entry replacement even when the URL is shared', async () => {
  const first = entry({ media_url: 'https://example.com/clip.mp4', media_type: 'video/mp4' });
  const second = entry({ ...first, id: 2, title: 'Second clip' });
  const { rerender } = render(FeedReader, { entries: [first, second], index: 0, onClose: vi.fn() });
  const old = document.querySelector('video')!;
  await rerender({ index: 1 });
  expect(document.querySelector('video')).not.toBe(old);
  expect(old).not.toHaveAttribute('src');
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
  expect(HTMLMediaElement.prototype.load).toHaveBeenCalled();
  const current = document.querySelector('video')!;
  await rerender({ index: null });
  expect(current).not.toHaveAttribute('src');
});

it('releases article-body players on replacement and close', async () => {
  const first = entry({
    images: [],
    content: '<video src="https://example.com/body.mp4" controls></video>',
  });
  const second = entry({
    id: 2,
    images: [],
    content: '<audio controls><source src="https://example.com/body.mp3"></audio>',
  });
  const { rerender } = render(FeedReader, { entries: [first, second], index: 0, onClose: vi.fn() });
  const video = document.querySelector('video')!;
  await rerender({ index: 1 });
  expect(video).not.toHaveAttribute('src');
  const source = document.querySelector('source')!;
  await rerender({ index: null });
  expect(source).not.toHaveAttribute('src');
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(2);
  expect(HTMLMediaElement.prototype.load).toHaveBeenCalledTimes(2);
});

it('displays decode failure advice while preserving the original link', async () => {
  mount(entry({ media_url: 'https://example.com/clip.mp4', media_type: 'video/mp4' }));
  await fireEvent.error(document.querySelector('video')!);
  expect(screen.getByRole('alert')).toHaveTextContent('Open the original to try it there.');
  expect(screen.getAllByRole('link', { name: 'Open original' }).length).toBeGreaterThan(0);
});

it('does not suggest an original link when none exists', async () => {
  const item = entry({
    url: '',
    media_url: 'https://example.com/clip.mp4',
    media_type: 'video/mp4',
  });
  item.feed.site_url = '';
  mount(item);
  await fireEvent.error(document.querySelector('video')!);
  expect(screen.getByRole('alert')).toHaveTextContent(/^Your browser could not play this media\.$/);
});

it('destroys provider frames and requires Play again on return', async () => {
  const first = video();
  const { rerender } = render(FeedReader, {
    entries: [first, entry({ id: 2 })],
    index: 0,
    onClose: vi.fn(),
  });
  await fireEvent.click(screen.getByLabelText(/Play video/));
  const frame = document.querySelector('iframe')!;
  await rerender({ index: 1 });
  expect(frame.isConnected).toBe(false);
  await rerender({ index: 0 });
  expect(document.querySelector('iframe')).toBeNull();
  expect(screen.getByLabelText(/Play video/)).toBeTruthy();
});
