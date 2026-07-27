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

describe('file handover links', () => {
  // Web chat has no outbound attachment channel, so a task hands a file over
  // as a link to the authenticated download endpoint. If the sanitizer drops
  // that href the whole handover silently degrades to unclickable text.
  const url = '/istota/api/chat/files?path=%2FUsers%2Falice%2Fistota%2Freport.csv';

  it('keeps the relative download URL', () => {
    const html = renderMarkdown(`[report.csv](${url})`);
    expect(html).toContain('href="/istota/api/chat/files?path=');
    expect(html).toContain('report.csv');
  });

  it('preserves percent-encoding in the path query', () => {
    const html = renderMarkdown(`[Q3 report.csv](${url.replace('report', 'Q3%20report')})`);
    expect(html).toContain('Q3%20report.csv');
  });

  it('opens in a new tab with noopener', () => {
    const html = renderMarkdown(`[f](${url})`);
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('still refuses a javascript: href', () => {
    // Left as inert text rather than an anchor — the scheme survives in the
    // body, which is harmless; what matters is that no href is emitted.
    const html = renderMarkdown('[x](javascript:alert(1))');
    expect(html).not.toContain('href');
    expect(html).not.toContain('<a ');
  });
});
