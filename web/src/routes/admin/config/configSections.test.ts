import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const source = readFileSync(join(__dirname, '+page.svelte'), 'utf8');

function ruleBody(selector: string): string {
  const style = source.slice(source.indexOf('<style>'));
  const start = style.indexOf(`\n  ${selector} {`);
  return start < 0 ? '' : style.slice(start, style.indexOf('}', start));
}

// ISSUE-788: every config field is mounted at once, and the section cards are
// what lets the browser skip the ones off screen.
describe('admin config section cards', () => {
  it('mark each section card in the sections loop', () => {
    const loop = source.slice(source.indexOf('{#each sections as section'));
    expect(loop).toMatch(
      /^\{#each sections as section[^}]*\}\s*<Card title=\{section\.label\} class="config-section">/,
    );
  });

  it('skip off-screen rendering and remember their height', () => {
    const body = ruleBody('.config-page :global(.config-section)');
    expect(body).toMatch(/content-visibility:\s*auto;/);
    expect(body).toMatch(/contain-intrinsic-size:\s*auto\s+\d+(\.\d+)?rem;/);
  });
});
