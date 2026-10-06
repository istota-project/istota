<script lang="ts">
  import { fileLinks } from '$lib/fileViewer/links';
  import hljs from 'highlight.js/lib/common';
  import { Button, TextArea } from '$lib/components/ui';
  import { renderDocument } from '$lib/markdown';
  import {
    presentationFor,
    splitFrontmatter,
    HIGHLIGHT_MAX_CHARS,
  } from '$lib/fileViewer/presentation';

  let {
    name,
    text,
    source = $bindable(false),
  }: {
    name: string;
    text: string;
    source?: boolean;
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

{#if presentation.kind === 'markdown'}
  <div class="view-controls" role="group" aria-label="Document view">
    <Button variant={source ? 'ghost' : 'secondary'} onclick={() => (source = false)}
      >Rendered</Button
    >
    <Button variant={source ? 'secondary' : 'ghost'} onclick={() => (source = true)}>Source</Button>
  </div>
{/if}
<div use:fileLinks class="file-body">
  {#if presentation.kind === 'markdown' && !source}
    <div class="markdown prose">
      {#if split.frontmatter !== null}<details>
          <summary>Frontmatter</summary>
          <pre>{split.frontmatter}</pre>
        </details>{/if}
      {@html renderDocument(split.body)}
    </div>
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
    />
  {/if}
</div>

<style>
  .view-controls {
    display: flex;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }
  .file-body {
    font-size: var(--text-base);
    overflow-wrap: anywhere;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: var(--font-mono);
  }
  details pre,
  pre code {
    font-size: var(--text-sm);
  }
</style>
