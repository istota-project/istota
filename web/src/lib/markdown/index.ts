/**
 * Markdown renderer for chat messages, built on markdown-it.
 *
 * Safe by construction: the parser runs with `html: false`, so any raw HTML in
 * the source is escaped rather than passed through. The only tags that reach
 * the DOM are the ones markdown-it emits itself (a fixed, known set), which is
 * why we can use `{@html}` on the output without a separate sanitizer pass.
 * Link hrefs are additionally restricted to an http/https/mailto/relative
 * allowlist via `validateLink`, and every link gets `target`/`rel` hardening.
 *
 * Supports the full CommonMark grammar plus markdown-it's built-in GFM tables
 * and strikethrough: fenced/indented code, inline code, bold, italic, strike,
 * links, autolinks, headings, nested ordered/unordered lists, blockquotes,
 * tables, and paragraphs.
 */
import MarkdownIt from 'markdown-it';

const md = new MarkdownIt({
	html: false, // never emit raw HTML from source — safe-by-construction
	linkify: true, // auto-link bare URLs
	breaks: true, // single newline -> <br>, which reads better in chat
	typographer: false,
});

// Disable linkify's fuzzy (schema-less) link detection. Without this, bare
// tokens like `FILENAME.md` get auto-linked because `.md` is a real TLD
// (Moldova) — chat text is full of `something.md` filenames that must stay
// plain text. Bare URLs that carry an explicit http(s)://  scheme still linkify.
md.linkify.set({ fuzzyLink: false, fuzzyEmail: false });

const SAFE_URL = /^(https?:\/\/|mailto:|\/)/i;

// Restrict link + image hrefs to a safe scheme allowlist. markdown-it already
// blocks javascript:/vbscript:/etc.; this tightens it to exactly what chat
// content should ever produce.
md.validateLink = (url: string): boolean => SAFE_URL.test(url.trim());

// Open links in a new tab with noopener/noreferrer. We layer onto the default
// renderer rather than replacing it so URL normalization/encoding still runs.
const defaultLinkOpen =
	md.renderer.rules.link_open ??
	((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
	const token = tokens[idx];
	token.attrSet('target', '_blank');
	token.attrSet('rel', 'noopener noreferrer');
	return defaultLinkOpen(tokens, idx, options, env, self);
};

export function renderMarkdown(src: string): string {
	if (!src) return '';
	return md.render(src);
}
