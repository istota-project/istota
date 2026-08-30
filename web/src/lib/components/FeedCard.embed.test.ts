/**
 * Playable media in a feed card (Are.na Embed blocks — YouTube, Vimeo).
 *
 * Before this, an Embed block reached the reader with no image, no body and
 * often no title, and painted a card that was blank but for the feed name and
 * date. The contract now: the block's thumbnail is the card image, and the
 * card offers a play affordance that swaps in a real player in place —
 * *without* the click also opening the image lightbox, which is what the
 * hero click normally does.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import type { FeedEntry } from '$lib/api';
import FeedCard from './FeedCard.svelte';

afterEach(cleanup);

function entry(over: Partial<FeedEntry> = {}): FeedEntry {
  return {
    id: 1,
    title: 'The Working Sheepdog',
    url: 'https://www.are.na/block/76969',
    content: '<p>Border Collies in training</p>',
    images: ['https://cdn.are.na/76969/thumb.jpg'],
    duplicate_image_count: 0,
    embed_url: 'https://www.youtube.com/watch?v=B0sO1wdBhMY',
    file_url: '',
    media_url: '',
    media_type: '',
    feed: {
      id: 1,
      title: 'arena-influences',
      site_url: 'https://www.are.na/channel/arena-influences',
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

describe('a card for playable media', () => {
  it('is not blank — it shows the thumbnail and the title', () => {
    const { container, getByText } = mount(entry());
    expect(container.querySelector('img')).toBeTruthy();
    expect(getByText('The Working Sheepdog')).toBeTruthy();
  });

  it('offers a play control', () => {
    const { getByLabelText } = mount(entry());
    expect(getByLabelText(/play/i)).toBeTruthy();
  });

  it('does not open the lightbox when the hero is clicked', async () => {
    let lightboxOpened = false;
    const { getByLabelText } = mount(entry(), () => {
      lightboxOpened = true;
    });

    await fireEvent.click(getByLabelText(/play/i));

    expect(lightboxOpened).toBe(false);
  });

  it('swaps in a player on click', async () => {
    const { getByLabelText, container } = mount(entry());
    expect(container.querySelector('iframe')).toBeNull();

    await fireEvent.click(getByLabelText(/play/i));

    const frame = container.querySelector('iframe');
    expect(frame).toBeTruthy();
    expect(frame?.getAttribute('src')).toContain(
      'https://www.youtube-nocookie.com/embed/B0sO1wdBhMY',
    );
  });

  it('plays vimeo too', async () => {
    const { getByLabelText, container } = mount(entry({ embed_url: 'https://vimeo.com/78314194' }));

    await fireEvent.click(getByLabelText(/play/i));

    expect(container.querySelector('iframe')?.getAttribute('src')).toContain(
      'https://player.vimeo.com/video/78314194',
    );
  });
});

describe('a card whose media we cannot embed', () => {
  it('keeps the ordinary lightbox behaviour for a plain image entry', async () => {
    let lightboxOpened = false;
    const { container } = mount(entry({ embed_url: '' }), () => {
      lightboxOpened = true;
    });

    const heroButton = container.querySelector('button.card-image') as HTMLElement;
    await fireEvent.click(heroButton);

    expect(lightboxOpened).toBe(true);
  });

  it('shows no player for an unknown provider, leaving the link in the body', () => {
    const { container } = mount(entry({ embed_url: 'https://example.com/watch/1' }));
    // No play affordance we can't honour…
    expect(container.querySelector('.card-video')).toBeNull();
    // …and the card is still not blank.
    expect(container.querySelector('img')).toBeTruthy();
  });

  it('still offers playback when the block had no thumbnail', async () => {
    const { getByLabelText, container } = mount(entry({ images: [] }));

    await fireEvent.click(getByLabelText(/play/i));

    expect(container.querySelector('iframe')).toBeTruthy();
  });
});

describe('player iframe hardening', () => {
  it('does not leak the referring page, and cannot navigate the reader away', async () => {
    const { getByLabelText, container } = mount(entry());
    await fireEvent.click(getByLabelText(/play/i));

    const frame = container.querySelector('iframe')!;
    expect(frame.getAttribute('referrerpolicy')).toBe('strict-origin-when-cross-origin');

    // `allow-same-origin` is required for the player to reach its *own*
    // storage and is not a hole in ours (the frame is cross-origin either
    // way). Top-level navigation is the property that actually matters: an
    // embed must never be able to redirect the whole reader.
    const sandbox = frame.getAttribute('sandbox') ?? '';
    expect(sandbox).toBeTruthy();
    expect(sandbox).not.toContain('allow-top-navigation');
    expect(sandbox).not.toContain('allow-forms');
  });

  it('titles the frame for screen readers', async () => {
    const { getByLabelText, container } = mount(entry());
    await fireEvent.click(getByLabelText(/play/i));

    expect(container.querySelector('iframe')?.getAttribute('title')).toBeTruthy();
  });
});
