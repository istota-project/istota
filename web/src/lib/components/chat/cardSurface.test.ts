import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { readLayer } from '$lib/styles/cascade';

/**
 * The fill of a block that sits inside the chat transcript: a received mail,
 * an external turn, the activity chip, a citation (ISSUE-671).
 *
 * These used `--surface-badge`, the pill fill, which in light is a mid grey on
 * the white transcript, and the citation used `--surface-card`, which in dark
 * *is* the transcript (`--surface-reading` is `var(--surface-card)` there) and
 * so drew no card at all. `--surface-reading-card` is the one fill for a card
 * on the reading surface, sized per theme against that surface.
 */

const CHAT = join(process.cwd(), 'src', 'lib', 'components', 'chat');

function ruleBody(file: string, selector: string): string {
  const source = readFileSync(join(CHAT, file), 'utf8');
  const at = source.indexOf(`${selector} {`);
  expect(at, `${file} has no ${selector} rule`).not.toBe(-1);
  return source.slice(at, source.indexOf('}', at));
}

const CARDS: [string, string][] = [
  ['MailCard.svelte', '  .mail-card'],
  ['Message.svelte', '  .external'],
  ['Message.svelte', '  .reply-quote'],
  ['ActivityTrace.svelte', '  .activity'],
];

/** Every value one theme block gives a custom property. */
function themeValue(css: string, blockSelector: string, name: string): string | undefined {
  const at = css.indexOf(`${blockSelector} {`);
  const body = css.slice(at, css.indexOf('\n}', at));
  return new RegExp(`${name}:\\s*([^;]+);`).exec(body)?.[1].trim();
}

describe('cards in the chat transcript', () => {
  it.each(CARDS)('%s %s fills with the reading-surface card', (file, selector) => {
    expect(ruleBody(file, selector)).toMatch(/background:\s*var\(--surface-reading-card\)/);
  });

  it('keeps the live activity chip on the same fill', () => {
    const source = readFileSync(join(CHAT, 'ActivityTrace.svelte'), 'utf8');
    const style = source.slice(source.indexOf('<style>'));
    expect(style).not.toMatch(/--surface-badge|--surface-card\b/);
    expect(ruleBody('ActivityTrace.svelte', '  .activity.active')).toMatch(
      /var\(--surface-reading-card\) 20%[\s\S]*var\(--surface-reading-card\) 80%/,
    );
  });

  it('differs from the reading surface in both themes', () => {
    const tokens = readLayer('tokens');
    for (const block of [':root', ":root[data-theme='light']"]) {
      const card = themeValue(tokens, block, '--surface-reading-card');
      const reading = themeValue(tokens, block, '--surface-reading');
      expect(card, `${block} defines --surface-reading-card`).toBeDefined();
      expect(card).not.toBe(reading);
      // In dark the reading surface is the card surface by reference.
      if (reading === 'var(--surface-card)') {
        expect(card).not.toBe(themeValue(tokens, block, '--surface-card'));
      }
    }
  });
});
