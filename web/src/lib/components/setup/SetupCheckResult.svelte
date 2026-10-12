<script lang="ts">
  interface Line {
    key: string;
    ok: boolean;
    warning?: boolean;
    message: string;
  }

  interface Props {
    /** A Test button's answer; nothing is drawn until there is one. `lines`
     *  are a check's separate steps, each drawn under the summary. */
    result: { ok: boolean; warning?: boolean; message: string; lines?: Line[] } | null;
  }

  let { result }: Props = $props();

  // A warning is something to read, whether or not the check counts it as
  // passed: the address check's "points somewhere else" is both.
  function tone(item: { ok: boolean; warning?: boolean }): 'ok' | 'warn' | 'bad' {
    if (item.warning) return 'warn';
    return item.ok ? 'ok' : 'bad';
  }

  const MARK = { ok: '✓', warn: '!', bad: '✗' } as const;
</script>

<!--
  A connection test's answer, in place under the fields it tested rather than
  as a notice: it is about this form, and it has to stay readable while the
  user fixes what it names.
-->
{#if result}
  <div class="check-result" role="status">
    <p class={tone(result)}>{result.message}</p>
    {#if result.lines?.length}
      <ul class="check-lines">
        {#each result.lines as line (line.key)}
          <li class={tone(line)} data-check-line={line.key}>
            <span class="mark" aria-hidden="true">{MARK[tone(line)]}</span>
            <span>{line.message}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}

<style>
  .check-result {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--text-sm);
  }

  p {
    margin: 0;
  }

  .check-lines {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .check-lines li {
    display: flex;
    gap: var(--space-2);
  }

  .mark {
    flex: none;
    width: 1em;
    text-align: center;
  }

  .ok {
    color: var(--status-success-fg);
  }

  .warn {
    color: var(--status-warn-fg);
  }

  .bad {
    color: var(--status-danger-fg);
  }
</style>
