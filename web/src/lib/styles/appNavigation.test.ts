import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { blockAfter, readLayer } from './cascade';

it('collapses the inline links and user name at 800px while the menu stays at every width', () => {
  const layout = readFileSync(resolve(process.cwd(), 'src/routes/+layout.svelte'), 'utf8');
  // The desktop menu is wanted (ISSUE-705): the shared nav-icon rule shows it at all widths.
  expect(blockAfter(layout, '.app-nav .nav-right .hamburger-btn') ?? '').not.toMatch(
    /display:\s*none;/,
  );
  const mobile = blockAfter(layout, '@media (max-width: 800px)') ?? '';
  expect(blockAfter(mobile, '.nav-user') ?? '').toMatch(/display:\s*none;/);
  const navigation = blockAfter(readLayer('markdown'), '@media (max-width: 800px)') ?? '';
  expect(blockAfter(navigation, '.app-nav .nav-links') ?? '').toMatch(/display:\s*none;/);
});
