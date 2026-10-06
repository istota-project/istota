import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import MediaPreview from './MediaPreview.svelte';

const failureMessage = 'Cannot play this media.';
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('shared native media preview', () => {
  it.each(['audio', 'video'] as const)(
    'plays %s only on request and releases it on close',
    async (kind) => {
      const { container, unmount } = render(MediaPreview, {
        kind,
        url: '/media.mp4',
        poster: '/still.jpg',
        failureMessage,
      });
      const player = container.querySelector(kind)!;
      expect(player).toHaveAttribute('src', '/media.mp4');
      expect(player).toHaveAttribute('controls');
      expect(player).toHaveAttribute('preload', 'metadata');
      expect(player).not.toHaveAttribute('autoplay');
      if (kind === 'video') {
        expect(player).toHaveAttribute('playsinline');
        expect(player).toHaveAttribute('poster', '/still.jpg');
      } else expect(player).not.toHaveAttribute('poster');
      await unmount();
      expect(player.pause).toHaveBeenCalled();
      expect(player.load).toHaveBeenCalled();
      expect(player).not.toHaveAttribute('src');
    },
  );

  it('shows the caller error and resets it on URL or kind replacement', async () => {
    const { container, rerender } = render(MediaPreview, {
      kind: 'audio',
      url: '/first.mp3',
      failureMessage,
    });
    const first = container.querySelector('audio')!;
    await fireEvent.error(first);
    expect(screen.getByRole('alert')).toHaveTextContent(failureMessage);
    expect(container.querySelector('audio')).toBeNull();
    expect(first).not.toHaveAttribute('src');
    await rerender({ url: '/second.mp3' });
    expect(screen.queryByRole('alert')).toBeNull();
    const second = container.querySelector('audio')!;
    expect(second).toHaveAttribute('src', '/second.mp3');
    await fireEvent.error(second);
    await rerender({ kind: 'video' });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(container.querySelector('video')).toHaveAttribute('src', '/second.mp3');
  });

  it('pauses and releases the previous player on replacement without clearing the new source', async () => {
    const { container, rerender } = render(MediaPreview, {
      kind: 'video',
      url: '/first.mp4',
      failureMessage,
    });
    const first = container.querySelector('video')!;
    await rerender({ url: '/second.mp4' });
    const second = container.querySelector('video')!;
    expect(first).not.toBe(second);
    expect(first.pause).toHaveBeenCalled();
    expect(first.load).toHaveBeenCalled();
    expect(first).not.toHaveAttribute('src');
    expect(second).toHaveAttribute('src', '/second.mp4');
  });
});
