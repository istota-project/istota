/**
 * A direct media attachment in a feed card (ISSUE-356).
 *
 * A Mastodon video arrives as a plain mp4 URL. The poller used to file it as
 * an image, so the card rendered `<img src="….mp4">` — a hero that never
 * decodes, over a lightbox that opens on nothing. The contract now: a stored
 * `media_url` gets a real `<video>`, it is bounded by CSS the way every other
 * piece of media in this card is, and clicking it plays rather than opening
 * the reader.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import type { FeedEntry } from '$lib/api';
import FeedCard from './FeedCard.svelte';

afterEach(cleanup);

function entry(over: Partial<FeedEntry> = {}): FeedEntry {
  return {
    id: 1,
    title: 'a clip',
    url: 'https://example.town/@someone/1',
    content: '<p>a clip</p>',
    images: [],
    duplicate_image_count: 0,
    embed_url: '',
    file_url: '',
    media_url: 'https://assets.example.town/media/117/original/clip.mp4',
    media_type: 'video/mp4',
    feed: {
      id: 1,
      title: 'a mastodon instance',
      site_url: 'https://example.town',
      category: null as never,
    },
    status: 'read', // avoids the IntersectionObserver path in trackView
    starred: false,
    starred_at: '',
    published_at: '2026-08-29T10:00:00Z',
    created_at: '2026-08-29T10:00:00Z',
    ...over,
  };
}

function mount(e: FeedEntry, onImageClick = () => {}) {
  return render(FeedCard, { props: { entry: e, onImageClick } });
}

describe('a card for a direct video attachment', () => {
  it('renders a <video>, not an <img>', () => {
    const { container } = mount(entry());
    const video = container.querySelector('video');
    expect(video).toBeTruthy();
    expect(video?.getAttribute('src')).toBe(
      'https://assets.example.town/media/117/original/clip.mp4',
    );
    // The regression: the mp4 must not reach an <img> anywhere on the card.
    expect(container.querySelector('img[src$=".mp4"]')).toBeNull();
  });

  it('gives the video controls and does not autoplay', () => {
    const { container } = mount(entry());
    const video = container.querySelector('video') as HTMLVideoElement;
    expect(video.hasAttribute('controls')).toBe(true);
    expect(video.hasAttribute('autoplay')).toBe(false);
    // A grid of cards must not each fetch a whole clip on scroll.
    expect(video.getAttribute('preload')).toBe('metadata');
  });

  it('carries no width or height attribute, so CSS alone bounds it', () => {
    // The sizing rule for feed media: bounded by the stylesheet, never by an
    // intrinsic attribute. A stored width is what ran a clip off the card.
    const { container } = mount(entry());
    const video = container.querySelector('video') as HTMLVideoElement;
    expect(video.hasAttribute('width')).toBe(false);
    expect(video.hasAttribute('height')).toBe(false);
  });

  it('uses a lone still from the entry as the poster', () => {
    const { container } = mount(
      entry({ images: ['https://assets.example.town/media/119/still.jpg'] }),
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    expect(video.getAttribute('poster')).toBe('https://assets.example.town/media/119/still.jpg');
    // Consumed as the poster, so not drawn a second time beside the player.
    expect(container.querySelector('.card-gallery')).toBeNull();
  });

  it('shows several stills rather than eating one as a poster', () => {
    // Taking images[0] as the poster would leave images[1..] rendered by
    // nothing — a card quietly showing fewer pictures than the entry has.
    const images = [
      'https://assets.example.town/a.jpg',
      'https://assets.example.town/b.jpg',
      'https://assets.example.town/c.jpg',
    ];
    const { container } = mount(entry({ images }));
    const video = container.querySelector('video') as HTMLVideoElement;
    expect(video.hasAttribute('poster')).toBe(false);
    expect(container.querySelectorAll('.card-gallery img')).toHaveLength(3);
  });

  it('never uses a playable URL as the poster', () => {
    // A downgrade to a pre-v7 binary re-files the clip into `images`. Using it
    // as the poster would put the mp4 back in an <img>-shaped hole.
    const { container } = mount(
      entry({ images: ['https://assets.example.town/media/117/original/clip.mp4'] }),
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    expect(video.hasAttribute('poster')).toBe(false);
  });

  it('does not open the reader when the video is clicked', async () => {
    let opened = false;
    const { container } = render(FeedCard, {
      props: { entry: entry(), onImageClick: () => {}, onOpen: () => (opened = true) },
    });
    await fireEvent.click(container.querySelector('video') as HTMLElement);
    expect(opened).toBe(false);
  });

  it('does not open the lightbox when the video is clicked', async () => {
    let lightbox = false;
    const { container } = mount(entry(), () => (lightbox = true));
    await fireEvent.click(container.querySelector('video') as HTMLElement);
    expect(lightbox).toBe(false);
  });

  it('renders an <audio> for an audio attachment', () => {
    const { container } = mount(
      entry({
        media_url: 'https://pod.example.com/audio/12.mp3',
        media_type: 'audio/mpeg',
      }),
    );
    expect(container.querySelector('audio')).toBeTruthy();
    expect(container.querySelector('video')).toBeNull();
  });

  it('falls back to the extension when the feed sent no MIME type', () => {
    const { container } = mount(entry({ media_type: '' }));
    expect(container.querySelector('video')).toBeTruthy();
  });

  it('plays nothing for a URL that is not http(s)', () => {
    // `media_url` is remote input and lands in a `src`. The component refuses
    // rather than trusting what the server stored.
    const { container } = mount(entry({ media_url: 'javascript:alert(1)' }));
    expect(container.querySelector('video')).toBeNull();
    expect(container.querySelector('audio')).toBeNull();
  });

  it('still shows the title and the body', () => {
    const { container } = mount(entry({ title: 'a clip', content: '<p>show notes</p>' }));
    expect(container.querySelector('.card-title-overlay')?.textContent?.trim()).toBe('a clip');
    expect(container.querySelector('.excerpt')?.textContent?.trim()).toBe('show notes');
  });

  it('lets a provider embed win over a direct file', () => {
    // Both is not a shape any feed produces today, but the order has to be
    // decided somewhere: the provider player is the more specific affordance.
    const { container } = mount(
      entry({ embed_url: 'https://www.youtube.com/watch?v=B0sO1wdBhMY' }),
    );
    expect(container.querySelector('video')).toBeNull();
    expect(container.querySelector('button.card-video')).toBeTruthy();
  });

  it('wins over an attached document', () => {
    const { container } = mount(entry({ file_url: 'https://example.com/paper.pdf' }));
    expect(container.querySelector('video')).toBeTruthy();
    expect(container.querySelector('.card-document')).toBeNull();
  });
});
