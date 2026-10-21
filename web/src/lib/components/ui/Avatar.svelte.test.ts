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

describe('Avatar: the picture it draws', () => {
  it('draws the URL it is given', () => {
    const { container } = render(Avatar, { src: '/pictures/alice.webp', label: 'Alice' });

    expect(picture(container)?.getAttribute('src')).toBe('/pictures/alice.webp');
  });

  it('draws the chip and asks for nothing without a URL', () => {
    for (const src of [null, '', undefined]) {
      const { container } = render(Avatar, { src, label: 'Istota' });

      expect(picture(container)).toBeNull();
      expect(chip(container)?.textContent?.trim()).toBe('I');
      cleanup();
    }
  });
});

describe('Avatar: the fallback', () => {
  it('takes over when the image fails, and gives way again when the URL changes', async () => {
    // Both halves are reachable: a request aborted by navigation fires `error`
    // in Chrome and WebKit, and an upload made offline fails and then succeeds
    // — which is exactly the moment the Settings preview swaps to a new hash.
    // Without the reset the chip is one-way for the life of the component.
    const { container, rerender } = render(Avatar, { src: '/alice?v=v1', label: 'Alice' });

    await fireEvent.error(picture(container)!);
    expect(picture(container)).toBeNull();
    expect(chip(container)?.textContent?.trim()).toBe('A');

    await rerender({ src: '/alice?v=v2', label: 'Alice' });

    expect(picture(container)?.getAttribute('src')).toBe('/alice?v=v2');
  });

  it('marks the bot chip so it keeps the amber fill it has today', () => {
    const { container } = render(Avatar, { kind: 'bot', label: 'Istota' });

    expect(chip(container)?.classList.contains('bot')).toBe(true);
  });

  it('falls back to a question mark rather than nothing on a blank label', () => {
    const { container } = render(Avatar, { label: '   ' });

    expect(chip(container)?.textContent?.trim()).toBe('?');
  });
});

describe('Avatar: what it must never do', () => {
  it('applies no filter, so an uploaded photograph is not rendered as a negative', () => {
    // `--sigil-filter` inverts a flat near-white silhouette for the light
    // theme. Running a face through it produces a negative, so the primitive
    // may not carry it — and the two sigil call sites keep it.
    expect(declarations).not.toMatch(/sigil-filter/);
    // `-webkit-filter` renders, so the property test has to reach the prefixed
    // spelling — `[^-\w]filter` excludes exactly the two forms that would still
    // produce the negative. `backdrop-filter` is not one of them and is allowed
    // through by the alternation being explicit rather than by a broad class.
    expect(styleBlock.replace(/\/\*[\s\S]*?\*\//g, '')).not.toMatch(
      /(?:^|[^\w-])(?:-webkit-)?filter\s*:/,
    );
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
    const { container: withImage } = render(Avatar, { src: '/alice', label: 'Alice' });
    expect(picture(withImage)?.getAttribute('alt')).toBe('');

    const { container: withChip } = render(Avatar, { label: 'Alice' });
    expect(chip(withChip)?.getAttribute('aria-hidden')).toBe('true');
  });

  it("carries the caller's alt on both states when one is given", () => {
    const { container: withImage } = render(Avatar, {
      src: '/alice',
      label: 'Alice',
      alt: 'Alice',
    });
    expect(picture(withImage)?.getAttribute('alt')).toBe('Alice');

    const { container: withChip } = render(Avatar, { label: 'Alice', alt: 'Alice' });
    expect(chip(withChip)?.getAttribute('aria-hidden')).toBeNull();
    expect(chip(withChip)?.getAttribute('aria-label')).toBe('Alice');
    expect(chip(withChip)?.getAttribute('role')).toBe('img');
  });
});
