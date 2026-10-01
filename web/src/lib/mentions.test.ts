import { describe, it, expect } from 'vitest';
import { codeRanges, findMentions, findPlainMentions, mentionText } from './mentions';

const people = [{ name: 'bob' }, { name: 'alice', self: true }];

describe('codeRanges', () => {
  it('finds an inline span and a fenced block, each closed by an equal run', () => {
    const text = 'a `x` b\n```\n@bob\n```\nc';
    const got = codeRanges(text).map(([s, e]) => text.slice(s, e));
    expect(got).toEqual(['`x`', '```\n@bob\n```']);
  });

  it('treats an unclosed run as text', () => {
    expect(codeRanges('a `b @bob')).toEqual([]);
  });

  it('needs the same run length to close', () => {
    const text = '``a ` @bob`` @alice';
    expect(codeRanges(text).map(([s, e]) => text.slice(s, e))).toEqual(['``a ` @bob``']);
  });

  it('never closes a run with a shorter one, which CommonMark leaves literal', () => {
    expect(codeRanges('```a @bob`` x')).toEqual([]);
    expect(findPlainMentions('```a @bob`` x', people).map((s) => s.target.name)).toEqual(['bob']);
  });

  it('stays linear on a long run of backticks a member can send', () => {
    // A backreference regex took ~1.4s on this shape at the server's 32k cap.
    const text = '@alice ' + '`'.repeat(16000) + 'x'.repeat(16000);
    const t0 = performance.now();
    expect(findPlainMentions(text, people).map((s) => s.target.name)).toEqual(['alice']);
    expect(performance.now() - t0).toBeLessThan(100);
  });
});

describe('the boundary', () => {
  it('treats a non-ASCII letter as part of a word, as the server does', () => {
    expect(findMentions('é@bob @bobé', people)).toEqual([]);
    expect(findMentions('é @bob', people).map((s) => s.target.name)).toEqual(['bob']);
  });
});

describe('findPlainMentions', () => {
  it('skips mentions inside backtick code', () => {
    const got = findPlainMentions('`@bob` @alice ```\n@bob\n``` @bob', people);
    expect(got.map((s) => s.target.name)).toEqual(['alice', 'bob']);
  });
});

describe('mentionText', () => {
  it('produces text the matcher reads back as the same mention', () => {
    const text = `hi ${mentionText('bob')} `;
    expect(findMentions(text, people).map((s) => s.target.name)).toEqual(['bob']);
  });
});
