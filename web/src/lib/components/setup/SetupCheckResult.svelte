<script lang="ts">
  interface Props {
    /** A Test button's answer; nothing is drawn until there is one. */
    result: { ok: boolean; warning?: boolean; message: string } | null;
  }

  let { result }: Props = $props();
</script>

<!--
  A connection test's answer, in place under the fields it tested rather than
  as a notice: it is about this form, and it has to stay readable while the
  user fixes what it names.
-->
{#if result}
  <p
    class="check-result"
    class:ok={result.ok && !result.warning}
    class:warn={result.ok && result.warning}
    class:bad={!result.ok}
    role="status"
  >
    {result.message}
  </p>
{/if}

<style>
  .check-result {
    margin: 0;
    font-size: var(--text-sm);
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
