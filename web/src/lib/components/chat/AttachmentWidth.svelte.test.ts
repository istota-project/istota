import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));

function sourceOf(file: string): string {
  return readFileSync(resolve(here, file), 'utf8');
}

function styleOf(file: string): string {
  const source = sourceOf(file);
  const open = source.indexOf('>', source.indexOf('<style'));
  return source.slice(open + 1, source.lastIndexOf('</style>')).replace(/\/\*[\s\S]*?\*\//g, '');
}

function rule(css: string, selector: string): string {
  const body = css.match(new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`))?.[1];
  expect(body, `${selector} has no style rule`).toBeDefined();
  return body!;
}

function expectEllipsis(body: string): void {
  expect(body).toMatch(/min-width:\s*0/);
  expect(body).toMatch(/overflow:\s*hidden/);
  expect(body).toMatch(/text-overflow:\s*ellipsis/);
  expect(body).toMatch(/white-space:\s*nowrap/);
}

describe('attachment chip width', () => {
  it('lets a message attachment shrink to its column and ellipsizes its name', () => {
    const source = sourceOf('Message.svelte');
    const css = styleOf('Message.svelte');
    expect(source).toMatch(
      /<div class="attachments">[\s\S]*?<(?:a|span) class="attachment(?: attachment-link)?"/,
    );
    expect(rule(css, '.attachments')).toMatch(/min-width:\s*0/);
    const attachment = rule(css, '.attachment');
    expect(attachment).toMatch(/max-width:\s*100%/);
    expectEllipsis(attachment);
  });

  it('ellipsizes only the staged filename so its remove button remains visible', () => {
    const source = sourceOf('Composer.svelte');
    const css = styleOf('Composer.svelte');
    expect(source).toMatch(
      /<span class="attach-chip"[\s\S]*?<span class="attach-name">\{att\.name\}<\/span>\s*<button\s+class="attach-x"/,
    );
    expect(rule(css, '.attach-row')).toMatch(/min-width:\s*0/);
    const chip = rule(css, '.attach-chip');
    expect(chip).toMatch(/max-width:\s*100%/);
    expect(chip).toMatch(/min-width:\s*0/);
    expectEllipsis(rule(css, '.attach-name'));
    expect(rule(css, '.attach-x')).toMatch(/flex:\s*0 0 auto/);
  });
});
