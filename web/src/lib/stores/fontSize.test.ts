/**
 * Font-size preference store.
 *
 * Mirrors the theme store: a client-local, localStorage-persisted setting
 * reflected onto <html> as `data-font-size` so the CSS root override applies.
 * `medium` is the default, so an absent or unrecognised stored value must
 * resolve to it — while `small` (the historical unscaled size) stays
 * explicitly selectable.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

// localStorage is stubbed once for every test file in vitest-setup.ts.

async function load() {
  vi.resetModules();
  return import('./fontSize');
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-font-size');
});

afterEach(() => {
  localStorage.clear();
});

describe('fontSize store', () => {
  it('defaults to medium when nothing is persisted', async () => {
    const { fontSize } = await load();
    expect(get(fontSize)).toBe('medium');
  });

  it.each([['small'], ['large']])('seeds from the persisted value %p', async (stored) => {
    localStorage.setItem('fontSize', JSON.stringify(stored));
    const { fontSize } = await load();
    expect(get(fontSize)).toBe(stored);
  });

  it.each([['huge'], [''], [null], [3]])(
    'falls back to medium for the unrecognised stored value %p',
    async (stored) => {
      localStorage.setItem('fontSize', JSON.stringify(stored));
      const { fontSize } = await load();
      expect(get(fontSize)).toBe('medium');
    },
  );

  it('survives a corrupt (non-JSON) stored value', async () => {
    localStorage.setItem('fontSize', '{not json');
    const { fontSize } = await load();
    expect(get(fontSize)).toBe('medium');
  });

  it('setFontSize updates the store, persists, and reflects onto <html>', async () => {
    const { fontSize, setFontSize } = await load();

    setFontSize('large');

    expect(get(fontSize)).toBe('large');
    expect(localStorage.getItem('fontSize')).toBe(JSON.stringify('large'));
    expect(document.documentElement.getAttribute('data-font-size')).toBe('large');
  });

  // `small` is no longer the fallback, so it has to be explicitly selectable —
  // a normalizer that treats "not medium/large" as the default would swallow it.
  it('setFontSize honours an explicit small', async () => {
    const { fontSize, setFontSize } = await load();

    setFontSize('small');

    expect(get(fontSize)).toBe('small');
    expect(localStorage.getItem('fontSize')).toBe(JSON.stringify('small'));
    expect(document.documentElement.getAttribute('data-font-size')).toBe('small');
  });

  it('setFontSize normalises an unknown value to medium', async () => {
    const { fontSize, setFontSize } = await load();

    setFontSize('gigantic' as never);

    expect(get(fontSize)).toBe('medium');
    expect(document.documentElement.getAttribute('data-font-size')).toBe('medium');
  });

  it('applyFontSize reflects without persisting', async () => {
    const { applyFontSize } = await load();

    applyFontSize('large');

    expect(document.documentElement.getAttribute('data-font-size')).toBe('large');
    expect(localStorage.getItem('fontSize')).toBeNull();
  });

  it('exposes the three selectable sizes in order', async () => {
    const { FONT_SIZES } = await load();
    expect(FONT_SIZES).toEqual(['small', 'medium', 'large']);
  });
});

describe('font-size CSS', () => {
  const css = readFileSync(resolve(__dirname, '../../app.css'), 'utf8');

  // The store is inert without a matching root rule — guard against the
  // attribute being set while nothing scales.
  it.each([['medium'], ['large']])('app.css scales the root for %s', (size) => {
    const rule = new RegExp(`:root\\[data-font-size='${size}'\\]\\s*\\{[^}]*font-size:`, 'm');
    expect(css).toMatch(rule);
  });

  it('leaves small unscaled (no root font-size override)', () => {
    expect(css).not.toMatch(/:root\[data-font-size='small'\]\s*\{[^}]*font-size:/m);
  });
});
