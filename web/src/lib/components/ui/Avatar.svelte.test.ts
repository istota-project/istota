import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import Avatar from './Avatar.svelte';

afterEach(cleanup);

/*
 * Two of these read the component's own source rather than the DOM, in the
 * spirit of the token invariants in `lib/styles`: what they assert is the
 * absence of a declaration, and an absence has no rendered form to query under
 * jsdom, which applies no scoped stylesheet.
 *
 * Resolved off this module's directory rather than `new URL('./…',
 * import.meta.url)` — Vite rewrites that exact pattern into an asset reference,
 * which under the test server resolves to an http: URL rather than the file.
 */
const source = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), 'Avatar.svelte'),
  'utf8',
);
const styleOpen = source.indexOf('>', source.indexOf('<style'));
const styleBlock = source.slice(styleOpen + 1, source.lastIndexOf('</style>'));
// Comments out, in all three of the file's syntaxes. Both absences below are
// worth *explaining* where they hold, and prose naming the thing it forbids
// would otherwise turn the assertion red for saying so — the design lint has
// the same carve-out, for the same reason.
const declarations = source
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '');

const picture = (c: HTMLElement) => c.querySelector('img');
const chip = (c: HTMLElement) => c.querySelector('.fallback');

describe('Avatar: the URL it asks for', () => {
  it('carries the content hash when the caller knows one', () => {
    const { container } = render(Avatar, {
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
    const { container } = render(Avatar, { kind: 'user', userId: 'bob', label: 'Bob' });

    expect(picture(container)?.getAttribute('src')).toBe('/api/avatars/user/bob');
  });

  it('reads the bot icon off its own endpoint', () => {
    const { container } = render(Avatar, { kind: 'bot', version: 'cd34', label: 'Istota' });

    expect(picture(container)?.getAttribute('src')).toBe('/api/avatars/bot?v=cd34');
  });

  it('asks for nothing at all when the payload said there is no picture', () => {
    // `null` is not "unknown": it is `/me` saying the hash is absent, which is
    // every deployment that has never had an avatar. A request there would be a
    // 404 per identity per page load.
    const { container } = render(Avatar, {
      kind: 'bot',
      version: null,
      label: 'Istota',
    });

    expect(picture(container)).toBeNull();
    expect(chip(container)?.textContent?.trim()).toBe('I');
  });

  it('renders the chip rather than an unbuildable URL when a user avatar has no id', () => {
    // `avatarUrl` throws on this pairing, so the guard has to be here: a user
    // row whose author the server has not named yet (Stage 6) still renders.
    const { container } = render(Avatar, { kind: 'user', version: 'ab12', label: 'Someone' });

    expect(picture(container)).toBeNull();
    expect(chip(container)?.textContent?.trim()).toBe('S');
  });
});

describe('Avatar: the fallback', () => {
  it('takes over when the image fails, and gives way again when the version changes', async () => {
    // Both halves are reachable: a request aborted by navigation fires `error`
    // in Chrome and WebKit, and an upload made offline fails and then succeeds
    // — which is exactly the moment the Settings preview swaps to a new hash.
    // Without the reset the chip is one-way for the life of the component.
    const { container, rerender } = render(Avatar, {
      kind: 'user',
      userId: 'alice',
      version: 'v1',
      label: 'Alice',
    });

    await fireEvent.error(picture(container)!);
    expect(picture(container)).toBeNull();
    expect(chip(container)?.textContent?.trim()).toBe('A');

    await rerender({ kind: 'user', userId: 'alice', version: 'v2', label: 'Alice' });

    expect(picture(container)?.getAttribute('src')).toBe('/api/avatars/user/alice?v=v2');
  });

  it('marks the bot chip so it keeps the amber fill it has today', () => {
    const { container } = render(Avatar, { kind: 'bot', version: null, label: 'Istota' });

    expect(chip(container)?.classList.contains('bot')).toBe(true);
  });

  it('falls back to a question mark rather than nothing on a blank label', () => {
    const { container } = render(Avatar, { kind: 'user', version: null, label: '   ' });

    expect(chip(container)?.textContent?.trim()).toBe('?');
  });
});

describe('Avatar: what it must never do', () => {
  it('applies no filter, so an uploaded photograph is not rendered as a negative', () => {
    // `--sigil-filter` inverts a flat near-white silhouette for the light
    // theme. Running a face through it produces a negative, so the primitive
    // may not carry it — and the two sigil call sites keep it.
    expect(declarations).not.toMatch(/sigil-filter/);
    expect(styleBlock.replace(/\/\*[\s\S]*?\*\//g, '')).not.toMatch(/[^-\w]filter\s*:/);
  });

  it('names no chat token, so the gutter is a call site rather than the default', () => {
    // The whole point of the `--avatar-size` indirection: a primitive that
    // reads `--chat-avatar` has the chat gutter's sizing baked into it, and
    // every later render site inherits a size that belongs to a transcript.
    expect(declarations).not.toMatch(/--chat-/);
  });
});

describe('Avatar: the accessible name', () => {
  it('is decorative by default, in both states', () => {
    // Most call sites render the name beside the image, so repeating it is
    // noise. The two states have to agree, or a picture that fails to load
    // changes what a screen reader announces.
    const { container: withImage } = render(Avatar, {
      kind: 'user',
      userId: 'alice',
      version: 'ab12',
      label: 'Alice',
    });
    expect(picture(withImage)?.getAttribute('alt')).toBe('');

    const { container: withChip } = render(Avatar, {
      kind: 'user',
      version: null,
      label: 'Alice',
    });
    expect(chip(withChip)?.getAttribute('aria-hidden')).toBe('true');
  });

  it("carries the caller's alt on both states when one is given", () => {
    const { container: withImage } = render(Avatar, {
      kind: 'user',
      userId: 'alice',
      version: 'ab12',
      label: 'Alice',
      alt: 'Alice',
    });
    expect(picture(withImage)?.getAttribute('alt')).toBe('Alice');

    const { container: withChip } = render(Avatar, {
      kind: 'user',
      version: null,
      label: 'Alice',
      alt: 'Alice',
    });
    expect(chip(withChip)?.getAttribute('aria-hidden')).toBeNull();
    expect(chip(withChip)?.getAttribute('aria-label')).toBe('Alice');
    expect(chip(withChip)?.getAttribute('role')).toBe('img');
  });
});
