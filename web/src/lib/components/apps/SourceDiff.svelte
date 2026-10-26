<script lang="ts">
  /* The source of a pending install against the current version, one block per
     file, as the server diffed it (`difflib.unified_diff`; a first install is
     every line added). Monospace and scrolling sideways rather than wrapping,
     so a long line reads as it is on a phone too. Each line is a text node:
     the source is the app's, and nothing here treats it as markup. */
  import { Badge } from '@minot/ui';
  import type { FileDiff } from '$lib/apps/api';

  let { files }: { files: FileDiff[] } = $props();

  const STATUS: Record<FileDiff['status'], string> = {
    added: 'Added',
    removed: 'Removed',
    changed: 'Changed',
    unchanged: 'Unchanged',
  };

  function lineKind(line: string): 'add' | 'del' | 'hunk' | 'head' | '' {
    if (line.startsWith('+++') || line.startsWith('---')) return 'head';
    if (line.startsWith('@@')) return 'hunk';
    if (line.startsWith('+')) return 'add';
    if (line.startsWith('-')) return 'del';
    return '';
  }

  function lines(diff: string): string[] {
    const out = diff.split('\n');
    if (out.length && out[out.length - 1] === '') out.pop();
    return out;
  }
</script>

<div class="vstack">
  {#each files as file (file.path)}
    <section class="vstack source-file" style:--vstack-gap="var(--space-2)">
      <div class="hstack wrap">
        <span class="mono">{file.path}</span>
        <Badge size="sm" tone="subtle" uppercase={false}>{STATUS[file.status]}</Badge>
      </div>
      {#if file.status !== 'unchanged' && file.diff}
        <pre class="source-diff"><code
            >{#each lines(file.diff) as line, i (i)}<span class="line {lineKind(line)}">{line}</span
              >{'\n'}{/each}</code
          ></pre>
      {/if}
    </section>
  {/each}
</div>

<style>
  .source-diff {
    margin: 0;
    padding: var(--space-2) var(--space-3);
    overflow-x: auto;
    white-space: pre;
    background: var(--surface-base);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    font-size: var(--text-xs);
    line-height: var(--leading-snug);
  }

  .line.add {
    color: var(--signed-pos);
  }

  .line.del {
    color: var(--signed-neg);
  }

  .line.hunk,
  .line.head {
    color: var(--text-dim);
  }
</style>
