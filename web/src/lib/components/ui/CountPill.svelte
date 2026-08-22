<script lang="ts">
  /**
   * A numeric badge, capped at `99+`.
   *
   * Renders **nothing at zero**, so no call site needs its own `{#if}` — "no
   * badge" is part of what a count pill means rather than a decision each
   * caller re-makes. Chat's `.unread-chip` is the shape this is lifted from and
   * is byte-identical to it apart from sizing on `--text-xs` (0.7rem) instead of
   * hardcoding the number, so migrating it is a token substitution with no
   * visual change. Note `--text-2xs` is 0.55rem and would shrink that chip by a
   * fifth while a "one fewer baselined violation" check still passed.
   */
  interface Props {
    count: number;
    /** Native tooltip. The count alone is not self-describing out of context. */
    title?: string;
    /** Screen-reader name. Falls back to `title`, then to the rendered digits —
     *  which are the truncated `99+` rather than the real number, hence the
     *  preference order. */
    label?: string;
  }

  let { count, title, label }: Props = $props();

  const shown = $derived(count > 99 ? '99+' : String(count));
</script>

{#if count > 0}
  <span class="count-pill" {title} aria-label={label ?? title ?? undefined}>{shown}</span>
{/if}

<style>
  .count-pill {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.15rem;
    height: 1.15rem;
    padding: 0 var(--space-2);
    border-radius: var(--radius-pill);
    background: var(--accent);
    color: var(--surface-base);
    font-size: var(--text-xs);
    font-weight: 600;
    line-height: 1;
  }
</style>
