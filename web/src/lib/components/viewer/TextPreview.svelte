<script lang="ts">
  import { fileLinks } from '$lib/fileViewer/links';
  import hljs from 'highlight.js/lib/common';
  import { TextArea } from '$lib/components/ui';
  import { pinViewportZoom, releaseViewportZoom } from '$lib/zoomPin';
  import { renderDocument, renderUntrustedDocument } from '$lib/markdown';
  import {
    presentationFor,
    splitFrontmatter,
    HIGHLIGHT_MAX_CHARS,
  } from '$lib/fileViewer/presentation';

  let {
    name,
    text,
    source = false,
    fill = false,
    untrusted = false,
  }: {
    name: string;
    text: string;
    /** Show a Markdown document as its readonly source. The caller owns the toggle. */
    source?: boolean;
    fill?: boolean;
    untrusted?: boolean;
  } = $props();
  let presentation = $derived(presentationFor(name));
  let split = $derived(splitFrontmatter(text));
  let language = $derived(presentation.kind === 'code' ? presentation.language : null);
  let highlighted = $derived(
    language && text.length <= HIGHLIGHT_MAX_CHARS
      ? hljs.highlight(text, { language, ignoreIllegals: true }).value
      : null,
  );
</script>

<div class="text-preview" class:fill>
  <div use:fileLinks class="file-body">
    {#if presentation.kind === 'markdown' && !source}
      <article class="document">
        {#if split.frontmatter !== null}<details class="frontmatter">
            <summary class="micro-label">Frontmatter</summary>
            <pre>{split.frontmatter}</pre>
          </details>{/if}
        <div class="markdown prose">
          {@html (untrusted ? renderUntrustedDocument : renderDocument)(split.body)}
        </div>
      </article>
    {:else if highlighted !== null}
      <div class="markdown"><pre><code class="hljs">{@html highlighted}</code></pre></div>
    {:else}
      <TextArea
        value={text}
        rows={18}
        monospace
        readonly
        spellcheck="false"
        aria-label={`Source of ${name}`}
        onfocus={pinViewportZoom}
        onblur={releaseViewportZoom}
      />
    {/if}
  </div>
</div>

<style>
  .fill {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .fill .file-body {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    overflow: auto;
  }
  .fill .markdown {
    padding: var(--space-2) var(--space-3);
  }
  /* A document in the viewer reads as a page: a measure that keeps lines
     followable on a wide screen, and room around it. */
  .fill .document {
    width: 100%;
    max-width: 46rem;
    margin: 0 auto;
    padding: var(--space-6) var(--space-6) var(--space-8);
  }
  .fill .document .markdown {
    padding: 0;
  }
  @media (max-width: 640px) {
    .fill .document {
      padding: var(--space-4) var(--space-3) var(--space-6);
    }
  }
  .fill :global(textarea) {
    flex: 1;
    min-height: 0;
    resize: none;
    padding: var(--space-2) var(--space-3);
    border: 0;
    border-radius: 0;
  }
  .file-body {
    /* Escapes the 16px touch floor; see TextFileEditor.svelte. */
    --file-text: var(--text-sm);
    font-size: var(--text-base);
    overflow-wrap: anywhere;
  }
  .file-body :global(textarea) {
    font-size: var(--file-text);
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: var(--font-mono);
  }
  pre code {
    font-size: var(--text-sm);
  }

  .frontmatter {
    margin: 0 0 var(--space-6);
    padding: var(--space-2) var(--space-3);
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }
  .frontmatter summary {
    cursor: pointer;
    color: var(--text-muted);
  }
  .frontmatter pre {
    margin: var(--space-2) 0 0;
    font-size: var(--text-xs);
    line-height: var(--leading-relaxed);
    color: var(--text-secondary);
  }

  /* `.markdown` is sized for chat turns, where every heading is body size. A
     file is a document, so its outline shows here. */
  .document .markdown {
    line-height: var(--leading-relaxed);
  }
  .document .markdown :global(h1),
  .document .markdown :global(h2),
  .document .markdown :global(h3),
  .document .markdown :global(h4) {
    line-height: var(--leading-tight);
    color: var(--text-primary);
  }
  .document .markdown :global(h1) {
    margin: var(--space-6) 0 var(--space-3);
    font-size: var(--text-xl);
  }
  .document .markdown :global(h2) {
    margin: var(--space-6) 0 var(--space-3);
    padding-bottom: var(--space-1);
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-lg);
  }
  .document .markdown :global(h3) {
    margin: var(--space-4) 0 var(--space-2);
    font-size: var(--text-base);
  }
  .document .markdown :global(h4) {
    margin: var(--space-4) 0 var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
  .document .markdown > :global(:first-child) {
    margin-top: 0;
  }
  .document .markdown :global(hr) {
    margin: var(--space-6) 0;
    border: 0;
    border-top: 1px solid var(--border-subtle);
  }
</style>
