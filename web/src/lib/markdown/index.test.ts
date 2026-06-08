import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './index';

describe('renderMarkdown syntax highlighting', () => {
	it('emits hljs token spans for a fenced block with a known language', () => {
		const html = renderMarkdown('```python\ndef f():\n    return 1\n```');
		// The <code> carries the hljs class so the theme palette applies.
		expect(html).toContain('class="hljs language-python"');
		// Keywords are wrapped in token spans by highlight.js.
		expect(html).toContain('hljs-keyword');
	});

	it('still renders an unknown language as an escaped plain code block', () => {
		const html = renderMarkdown('```nosuchlang\n<script>x</script>\n```');
		expect(html).toContain('class="hljs language-nosuchlang"');
		// Raw HTML in the code body must be escaped, not passed through.
		expect(html).toContain('&lt;script&gt;');
		expect(html).not.toContain('<script>x');
	});

	it('renders a bare fenced block (no language) without crashing', () => {
		const html = renderMarkdown('```\nplain text\n```');
		expect(html).toContain('<pre>');
		expect(html).toContain('class="hljs"');
		expect(html).toContain('plain text');
	});

	it('leaves inline code as a plain <code> (no hljs tokens)', () => {
		const html = renderMarkdown('use `print()` here');
		expect(html).toContain('<code>print()</code>');
		expect(html).not.toContain('hljs');
	});
});
