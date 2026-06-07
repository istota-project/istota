/**
 * Tiny, dependency-free Markdown renderer for chat bubbles.
 *
 * Safe by construction: every piece of source text is HTML-escaped *before*
 * any formatting tags are injected, so no source markup can ever reach the
 * DOM as live HTML. Only a fixed allowlist of tags is produced here, and link
 * hrefs are restricted to http/https/mailto/relative schemes. This is why we
 * can use `{@html}` on the output without a separate sanitizer pass.
 *
 * Supported: fenced code blocks, inline code, bold, italic, links, headings,
 * unordered/ordered lists, and paragraphs. Everything else renders as plain
 * (escaped) text.
 */

function escapeHtml(s: string): string {
	return s
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;');
}

const SAFE_URL = /^(https?:\/\/|mailto:|\/)/i;

function renderInline(escaped: string): string {
	// `escaped` is already HTML-escaped. We only add our own tags from here.
	// Split out inline code first so emphasis/links inside it are left alone.
	const parts = escaped.split(/(`[^`]+`)/g);
	return parts
		.map((part) => {
			if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
				return `<code>${part.slice(1, -1)}</code>`;
			}
			let out = part;
			// Links: [text](url) — url validated against the safe-scheme allowlist.
			out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
				// `url` is escaped text; &amp; etc. don't affect scheme detection.
				const raw = url.replace(/&amp;/g, '&');
				if (!SAFE_URL.test(raw)) return text;
				return `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`;
			});
			out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
			out = out.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
			return out;
		})
		.join('');
}

export function renderMarkdown(src: string): string {
	if (!src) return '';
	const lines = src.replace(/\r\n/g, '\n').split('\n');
	const html: string[] = [];

	let i = 0;
	let para: string[] = [];
	let list: { type: 'ul' | 'ol'; items: string[] } | null = null;

	const flushPara = () => {
		if (para.length) {
			html.push(`<p>${renderInline(escapeHtml(para.join('\n'))).replace(/\n/g, '<br>')}</p>`);
			para = [];
		}
	};
	const flushList = () => {
		if (list) {
			const items = list.items.map((it) => `<li>${renderInline(escapeHtml(it))}</li>`).join('');
			html.push(`<${list.type}>${items}</${list.type}>`);
			list = null;
		}
	};

	while (i < lines.length) {
		const line = lines[i];

		// Fenced code block.
		const fence = line.match(/^```(.*)$/);
		if (fence) {
			flushPara();
			flushList();
			const body: string[] = [];
			i++;
			while (i < lines.length && !lines[i].match(/^```/)) {
				body.push(lines[i]);
				i++;
			}
			i++; // consume closing fence
			html.push(`<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`);
			continue;
		}

		// Heading.
		const heading = line.match(/^(#{1,6})\s+(.*)$/);
		if (heading) {
			flushPara();
			flushList();
			const level = Math.min(heading[1].length, 4); // cap at h4 visually
			html.push(`<h${level}>${renderInline(escapeHtml(heading[2]))}</h${level}>`);
			i++;
			continue;
		}

		// List items.
		const ul = line.match(/^\s*[-*]\s+(.*)$/);
		const ol = line.match(/^\s*\d+\.\s+(.*)$/);
		if (ul || ol) {
			flushPara();
			const type = ul ? 'ul' : 'ol';
			const text = (ul ? ul[1] : ol![1]);
			if (!list || list.type !== type) {
				flushList();
				list = { type, items: [] };
			}
			list.items.push(text);
			i++;
			continue;
		}

		// Blank line ends a block.
		if (line.trim() === '') {
			flushPara();
			flushList();
			i++;
			continue;
		}

		// Paragraph text.
		flushList();
		para.push(line);
		i++;
	}
	flushPara();
	flushList();
	return html.join('');
}
