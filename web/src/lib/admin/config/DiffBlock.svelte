<script lang="ts">
  interface Props {
    /** A unified diff, one line per entry. */
    lines: string[];
  }

  let { lines }: Props = $props();

  function lineKind(line: string): string {
    if (line.startsWith('+++') || line.startsWith('---')) return 'file';
    if (line.startsWith('@@')) return 'hunk';
    if (line.startsWith('+')) return 'add';
    if (line.startsWith('-')) return 'del';
    return '';
  }
</script>

<!-- The redacted config.toml diff, as the review dialog and the history show it. -->
<pre class="diff">{#each lines as line, i (i)}<span class="line {lineKind(line)}">{line}</span
    >{/each}</pre>

<style>
  .diff {
    margin: 0;
    padding: var(--space-2);
    max-height: 20rem;
    overflow: auto;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    background: var(--surface-base);
    border-radius: var(--radius-sm);
    white-space: pre;
  }

  .line {
    display: block;
  }

  .add {
    color: var(--status-success-fg);
  }

  .del {
    color: var(--status-danger-fg);
  }

  .file,
  .hunk {
    color: var(--text-dim);
  }
</style>
