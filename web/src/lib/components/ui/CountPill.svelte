<script lang="ts">
  /**
   * A numeric badge, capped at `99+`.
   *
   * Renders **nothing at zero**, so no call site needs its own `{#if}` — "no
   * badge" is part of what a count pill means rather than a decision each
   * caller re-makes. Chat's `.unread-chip` was the shape this was lifted from,
   * byte-identical apart from sizing on `--text-xs` (0.7rem) instead of
   * hardcoding the number, and now uses this component (both chips in
   * `routes/chat/+page.svelte`). Keep the token: `--text-2xs` is 0.55rem and
   * would shrink both of those chips by a fifth.
   */
  interface Props {
    count: number;
    /** Native tooltip. The count alone is not self-describing out of context,
     *  and `title` is valid on any element — unlike `aria-label`, see below. */
    title?: string;
  }

  let { count, title }: Props = $props();

  const shown = $derived(count > 99 ? '99+' : String(count));
</script>

<!--
  No `aria-label`. This is a bare <span>, so its implicit role is `generic`,
  which does not support an accessible name — assistive tech drops the
  attribute, and a prop named "screen-reader name" that names nothing is worse
  than none, because it reads as covered. The digits stay as text content, which
  a generic element *does* expose, so the count reaches AT either way.

  Naming belongs on the interactive element that owns the pill, the way
  `IconButton` requires: `NotificationBell` puts the real, untruncated count in
  its button's own `aria-label`, which overrides descendant content entirely.
-->
{#if count > 0}
  <span class="count-pill" {title}>{shown}</span>
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
