import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { blockAfter, readLayer } from './cascade';

it('switches the top navigation links and menu together at 800px', () => {
  const layout = readFileSync(resolve(process.cwd(), 'src/routes/+layout.svelte'), 'utf8');
  // Match the shared nav-icon rule's specificity before hiding the menu.
  expect(blockAfter(layout, '.app-nav .nav-right .hamburger-btn') ?? '').toMatch(
    /display:\s*none;/,
  );
  const mobile = blockAfter(layout, '@media (max-width: 800px)') ?? '';
  expect(blockAfter(mobile, '.hamburger-btn') ?? '').toMatch(/display:\s*inline-flex;/);
  expect(blockAfter(mobile, '.nav-user') ?? '').toMatch(/display:\s*none;/);
  const navigation = blockAfter(readLayer('markdown'), '@media (max-width: 800px)') ?? '';
  expect(blockAfter(navigation, '.app-nav .nav-links') ?? '').toMatch(/display:\s*none;/);
});
