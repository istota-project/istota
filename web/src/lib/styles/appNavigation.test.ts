import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { blockAfter, readCascade, readLayer, stripComments } from './cascade';

it('collapses the inline links and user name below 1024px while the menu stays at every width', () => {
  const layout = readFileSync(resolve(process.cwd(), 'src/routes/+layout.svelte'), 'utf8');
  // The desktop menu is wanted (ISSUE-705): the shared nav-icon rule shows it at all widths.
  expect(blockAfter(layout, '.app-nav .nav-right .hamburger-btn') ?? '').not.toMatch(
    /display:\s*none;/,
  );
  const mobile = blockAfter(layout, '@media (max-width: 1023px)') ?? '';
  expect(blockAfter(mobile, '.nav-user') ?? '').toMatch(/display:\s*none;/);
  const navigation = blockAfter(readLayer('app-shell'), '@media (max-width: 1023px)') ?? '';
  expect(blockAfter(navigation, '.app-nav .nav-links') ?? '').toMatch(/display:\s*none;/);
});

// jsdom has no layout, so the height is pinned in the cascade: no breakpoint
// restates the nav's vertical padding (an 800px one once outlived the collapse it
// was written for, and the nav jumped there for nothing).
it('keeps the app nav padding the same at every width', () => {
  const css = stripComments(readCascade());
  const media = [...css.matchAll(/@media[^{]*\{([\s\S]*?)\n\}/g)].map((m) => m[1]);
  for (const body of media) {
    expect(blockAfter(body, '.app-nav {') ?? '').not.toMatch(/padding/);
  }
});

it('gives the app nav and the page bar one shared height', () => {
  const nav = blockAfter(stripComments(readLayer('app-shell')), '.app-nav {') ?? '';
  expect(nav).toMatch(/min-height:\s*calc\(var\(--app-bar-height\) \+ var\(--safe-top\)\);/);
  expect(nav).toMatch(/padding:\s*0 0\.75rem;/);
});

it('lets inline links wrap when badges and large text outgrow the desktop row', () => {
  const links = blockAfter(stripComments(readLayer('app-shell')), '.app-nav .nav-links {') ?? '';
  expect(links).toMatch(/flex-wrap:\s*wrap;/);
  expect(links).toMatch(/min-width:\s*0;/);
  const controls = blockAfter(stripComments(readLayer('app-shell')), '.app-nav .nav-right {') ?? '';
  expect(controls).toMatch(/flex-shrink:\s*0;/);
});

it('positions inline links so an unread mark sits out of flow and moves nothing', () => {
  const link = blockAfter(stripComments(readLayer('app-shell')), '.app-nav .nav-links a {') ?? '';
  expect(link).toMatch(/position:\s*relative;/);
});
