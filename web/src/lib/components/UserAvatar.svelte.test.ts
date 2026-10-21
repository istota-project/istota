import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import UserAvatar from './UserAvatar.svelte';

afterEach(cleanup);

// The real `avatarUrl`, not a stub: the URL is what this wrapper is for.
const picture = (c: HTMLElement) => c.querySelector('img');
const chip = (c: HTMLElement) => c.querySelector('.fallback');

describe('UserAvatar: the URL it asks for', () => {
  it('carries the content hash when the caller knows one', () => {
    const { container } = render(UserAvatar, {
      kind: 'user',
      userId: 'alice',
      version: 'ab12',
      label: 'Alice',
    });

    expect(picture(container)?.getAttribute('src')).toBe('/api/avatars/user/alice?v=ab12');
  });

  it('asks without a version when the caller does not know one', () => {
    // A third party's hash is in no payload the client holds (D13), so the
    // request goes out bare and pays one conditional round trip per author.
    const { container } = render(UserAvatar, { kind: 'user', userId: 'bob', label: 'Bob' });

    expect(picture(container)?.getAttribute('src')).toBe('/api/avatars/user/bob');
  });

  it('reads the bot icon off its own endpoint', () => {
    const { container } = render(UserAvatar, { kind: 'bot', version: 'cd34', label: 'Istota' });

    expect(picture(container)?.getAttribute('src')).toBe('/api/avatars/bot?v=cd34');
  });

  it('asks for nothing at all when the payload said there is no picture', () => {
    // `null` is not "unknown": it is `/me` saying the hash is absent, which is
    // every deployment that has never had an avatar. A request there would be a
    // 404 per identity per page load.
    const { container } = render(UserAvatar, { kind: 'bot', version: null, label: 'Istota' });

    expect(picture(container)).toBeNull();
    expect(chip(container)?.textContent?.trim()).toBe('I');
    expect(chip(container)?.classList.contains('bot')).toBe(true);
  });

  it('renders the chip rather than an unbuildable URL when a user avatar has no id', () => {
    // `avatarUrl` throws on this pairing, so the guard has to be here: a user
    // row whose author the server has not named yet still renders.
    const { container } = render(UserAvatar, { kind: 'user', version: 'ab12', label: 'Someone' });

    expect(picture(container)).toBeNull();
    expect(chip(container)?.textContent?.trim()).toBe('S');
  });

  it('passes the alt text through', () => {
    const { container } = render(UserAvatar, {
      kind: 'user',
      userId: 'alice',
      label: 'Alice',
      alt: 'Your profile picture',
    });

    expect(picture(container)?.getAttribute('alt')).toBe('Your profile picture');
  });
});
