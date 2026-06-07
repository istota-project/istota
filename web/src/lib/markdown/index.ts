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
 * unordered/ordered lists, tables, and paragraphs. Everything else renders as
 * plain (escaped) text.
 */

function escapeHtml(s: string): string {
	return s
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;');
}

const SAFE_URL = /^(https?:\/\/|mailto:|\/)/i;

function applyEmphasis(s: string): string {
	let out = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
	out = out.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
	return out;
}

function renderInline(escaped: string): string {
	// `escaped` is already HTML-escaped. We only add our own tags from here.
	// Split out inline code first so emphasis/links inside it are left alone.
	const parts = escaped.split(/(`[^`]+`)/g);
	return parts
		.map((part) => {
			if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
				return `<code>${part.slice(1, -1)}</code>`;
			}
			// Emit link HTML verbatim and emphasis-process only the gaps between
			// links (and a link's own visible text). Emphasis runs over the whole
			// string, so applying it after building links would let a `*` inside a
			// URL inject a <strong>/<em> tag into the href attribute value — this
			// walk keeps emphasis away from the href entirely.
			const linkRe = /\[([^\]]+)\]\(([^)\s]+)\)/g;
			let out = '';
			let last = 0;
			let m: RegExpExecArray | null;
			while ((m = linkRe.exec(part)) !== null) {
				out += applyEmphasis(part.slice(last, m.index));
				const [full, text, url] = m;
				// `url` is escaped text; &amp; etc. don't affect scheme detection.
				const raw = url.replace(/&amp;/g, '&');
				out += SAFE_URL.test(raw)
					? `<a href="${url}" target="_blank" rel="noopener noreferrer">${applyEmphasis(text)}</a>`
					: applyEmphasis(text);
				last = m.index + full.length;
			}
			out += applyEmphasis(part.slice(last));
			return out;
		})
		.join('');
}

function splitTableRow(line: string): string[] {
	return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

// Returns per-column alignment for a GitHub pipe-table separator row
// (e.g. `|:--|--:|:-:|`), or null when the line isn't a separator.
function parseTableAligns(sep: string): string[] | null {
	if (!sep.includes('|')) return null;
	const cells = splitTableRow(sep);
	if (!cells.length || !cells.every((c) => /^:?-+:?$/.test(c))) return null;
	return cells.map((c) =>
		c.startsWith(':') && c.endsWith(':') ? 'center'
		: c.endsWith(':') ? 'right'
		: c.startsWith(':') ? 'left' : '',
	);
}

// Alignment is from a fixed {left,right,center} set — safe to inline as style.
function alignAttr(a: string): string {
	return a ? ` style="text-align:${a}"` : '';
}

function renderTable(header: string[], aligns: string[], rows: string[][]): string {
	const th = header
		.map((c, j) => `<th${alignAttr(aligns[j] ?? '')}>${renderInline(escapeHtml(c))}</th>`)
		.join('');
	const body = rows
		.map((r) =>
			`<tr>${header
				.map((_c, j) => `<td${alignAttr(aligns[j] ?? '')}>${renderInline(escapeHtml(r[j] ?? ''))}</td>`)
				.join('')}</tr>`,
		)
		.join('');
	return `<table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`;
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

		// Table (GitHub pipe table): a header row with pipes followed by a
		// `---|:--:|--` separator row. Column count is fixed by the header;
		// extra body cells are dropped and missing ones render empty.
		const aligns = line.includes('|') && i + 1 < lines.length
			? parseTableAligns(lines[i + 1])
			: null;
		if (aligns) {
			flushPara();
			flushList();
			const header = splitTableRow(line);
			i += 2;
			const rows: string[][] = [];
			while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
				rows.push(splitTableRow(lines[i]));
				i++;
			}
			html.push(renderTable(header, aligns, rows));
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
