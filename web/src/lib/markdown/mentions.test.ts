/**
 * `@name` mentions in a rendered message (ISSUE-578).
 *
 * Only names the caller lists are styled, so a guest cannot dress arbitrary
 * text as a mention of a member, and the boundary is the speech gate's, so a
 * styled `@bot` is a message the gate reads as addressing the bot.
 */
import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './index';

const people = [{ name: 'alice', self: true }, { name: 'bob' }, { name: 'Istota' }];

describe('@mentions', () => {
  it('styles a mention of a listed name, the viewer more strongly', () => {
    const html = renderMarkdown('hi @bob and @alice', people);
    expect(html).toContain('<span class="mention">@bob</span>');
    expect(html).toContain('<span class="mention mention-self">@alice</span>');
  });

  it('matches the bot name case-insensitively, keeping the text as written', () => {
    expect(renderMarkdown('@istota what now?', people)).toContain(
      '<span class="mention">@istota</span>',
    );
  });

  it('leaves an unlisted @word alone', () => {
    expect(renderMarkdown('ping @mallory', people)).not.toContain('class="mention');
  });

  it('is not a mention inside a code span, a fenced block or a link', () => {
    const html = renderMarkdown('`@bob` [@bob](https://example.com/x)\n\n```\n@bob\n```', people);
    expect(html).not.toContain('class="mention');
    expect(html).toContain('<code>@bob</code>');
  });

  it('needs a boundary on both sides, as the speech gate does', () => {
    const html = renderMarkdown('bob@alice.example @bobby @bob-x @@bob', people);
    expect(html).not.toContain('class="mention');
  });

  it('reads an escaped character as the boundary the reader sees', () => {
    // `a\_@bob` shows as `a_@bob`: no boundary before the `@`.
    expect(renderMarkdown('a\\_@bob and @bob\\_x', people)).not.toContain('class="mention');
  });

  it('still matches inside emphasis and before punctuation', () => {
    const html = renderMarkdown('**@bob**, thanks @alice.', people);
    expect(html).toContain('<strong><span class="mention">@bob</span></strong>');
    expect(html).toContain('mention-self">@alice</span>.');
  });

  it('prefers the longer of two names that share a prefix', () => {
    const html = renderMarkdown('@al @alice', [{ name: 'al' }, { name: 'alice' }]);
    expect(html).toContain('<span class="mention">@al</span>');
    expect(html).toContain('<span class="mention">@alice</span>');
  });

  it('styles nothing without a list, and escapes what it matches', () => {
    expect(renderMarkdown('hi @bob')).not.toContain('class="mention');
    expect(renderMarkdown('@a<b', [{ name: 'a<b' }])).toContain(
      '<span class="mention">@a&lt;b</span>',
    );
  });
});
