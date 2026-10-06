import hljs from 'highlight.js/lib/common';

export type Presentation =
  { kind: 'markdown' } | { kind: 'code'; language: string } | { kind: 'plain' };

export const HIGHLIGHT_MAX_CHARS = 200_000;

const LANGUAGES: Record<string, string> = {
  py: 'python',
  ts: 'typescript',
  js: 'javascript',
  json: 'json',
  toml: 'ini',
  yaml: 'yaml',
  yml: 'yaml',
  sh: 'bash',
  css: 'css',
  html: 'xml',
  htm: 'xml',
  svg: 'xml',
  xml: 'xml',
  sql: 'sql',
  diff: 'diff',
  patch: 'diff',
  go: 'go',
  rs: 'rust',
};

/** Choose a presentation for text the server has already admitted. */
export function presentationFor(name: string): Presentation {
  const dot = name.lastIndexOf('.');
  const ext = dot < 0 ? '' : name.slice(dot + 1).toLowerCase();
  if (['md', 'markdown', 'mdown'].includes(ext)) return { kind: 'markdown' };
  const language = Object.hasOwn(LANGUAGES, ext) ? LANGUAGES[ext] : undefined;
  if (language && hljs.getLanguage(language)) return { kind: 'code', language };
  return { kind: 'plain' };
}

/** Split a leading fenced block without parsing YAML or changing its contents. */
export function splitFrontmatter(text: string): { frontmatter: string | null; body: string } {
  const opening = /^---\r?\n/.exec(text);
  if (!opening) return { frontmatter: null, body: text };
  const rest = text.slice(opening[0].length);
  const closing = /(^|\n)(?:---|\.\.\.)(?:\r?\n|(?![\s\S]))/.exec(rest);
  if (!closing) return { frontmatter: null, body: text };
  return {
    frontmatter: rest.slice(0, closing.index + closing[1].length),
    body: rest.slice(closing.index + closing[0].length),
  };
}
