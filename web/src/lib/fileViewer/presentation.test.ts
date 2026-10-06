import { afterEach, describe, expect, it, vi } from 'vitest';
import hljs from 'highlight.js/lib/common';
import { presentationFor, splitFrontmatter } from './presentation';

afterEach(() => vi.restoreAllMocks());

describe('presentationFor', () => {
  it.each(['md', 'markdown', 'mdown', 'MD'])('renders %s as markdown', (ext) => {
    expect(presentationFor(`note.${ext}`)).toEqual({ kind: 'markdown' });
  });

  it.each([
    ['py', 'python'],
    ['ts', 'typescript'],
    ['js', 'javascript'],
    ['json', 'json'],
    ['toml', 'ini'],
    ['yaml', 'yaml'],
    ['yml', 'yaml'],
    ['sh', 'bash'],
    ['css', 'css'],
    ['html', 'xml'],
    ['htm', 'xml'],
    ['svg', 'xml'],
    ['xml', 'xml'],
    ['sql', 'sql'],
    ['diff', 'diff'],
    ['patch', 'diff'],
    ['go', 'go'],
    ['rs', 'rust'],
    ['PY', 'python'],
  ])('uses the bundled %s language', (ext, language) => {
    expect(presentationFor(`file.${ext}`)).toEqual({ kind: 'code', language });
  });

  it.each([
    'README',
    'file.txt',
    'file.csv',
    'file.log',
    'file.tar.gz',
    'file.md.bak',
    'file.',
    '',
  ])('leaves %s plain', (name) => {
    expect(presentationFor(name)).toEqual({ kind: 'plain' });
  });

  it('uses only the final extension', () => {
    expect(presentationFor('file.test.ts')).toEqual({ kind: 'code', language: 'typescript' });
  });

  it('falls back to plain when the bundled highlighter lacks a language', () => {
    vi.spyOn(hljs, 'getLanguage').mockReturnValue(undefined);
    expect(presentationFor('script.py')).toEqual({ kind: 'plain' });
  });
});

describe('splitFrontmatter', () => {
  it.each(['---', '...'])('splits a block closed by %s', (end) => {
    expect(splitFrontmatter(`---\ntitle: Note\n${end}\n# Body\n`)).toEqual({
      frontmatter: 'title: Note\n',
      body: '# Body\n',
    });
  });

  it('preserves CRLF inside both sections', () => {
    expect(splitFrontmatter('---\r\ntitle: Note\r\n---\r\n# Body\r\n')).toEqual({
      frontmatter: 'title: Note\r\n',
      body: '# Body\r\n',
    });
  });

  it.each([
    '# Body',
    '',
    '\n---\ntitle: Note\n---',
    '---\ntitle: Note',
    '---\ntitle: Note\n--- ',
    '--- \ntitle: Note\n---',
  ])('leaves absent or unterminated frontmatter untouched: %j', (text) => {
    expect(splitFrontmatter(text)).toEqual({ frontmatter: null, body: text });
  });

  it('accepts a closing marker at EOF and an empty block', () => {
    expect(splitFrontmatter('---\n---')).toEqual({ frontmatter: '', body: '' });
  });

  it('does not parse YAML or match an indented terminator', () => {
    expect(splitFrontmatter('---\n  ---\n<script>\n...\nbody')).toEqual({
      frontmatter: '  ---\n<script>\n',
      body: 'body',
    });
  });
});
