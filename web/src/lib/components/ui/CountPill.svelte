<script lang="ts">
  import { Asterisk } from '@lucide/svelte';

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
    /**
     * How loudly it reads.
     *
     * `accent` is the badge as it has always been: something arrived and is
     * unread. `muted` is for a count that is a *state* rather than news — the
     * chat room list's "waiting to send" (ISSUE-202), which the user put there
     * themselves and which nothing new has happened to. A variant rather than
     * a second component, because everything else about the two is the same
     * and a copy would drift on the next sizing change.
     */
    tone?: 'accent' | 'muted';
    /**
     * `pill` sits in the row's flow. `mark` is the inline app nav's: a small
     * unread glyph, no digits, taken out of flow past the end of the parent,
     * which must be positioned, so it appearing or clearing moves no link.
     * `inline-mark` is the same glyph in the row's flow, for a list row whose
     * mark ends a line (the briefings archive's audience line) or a row (the
     * feeds sidebar). Both are `--unread-mark-size`, so the asterisk is one size
     * wherever it appears.
     */
    shape?: 'pill' | 'mark' | 'inline-mark';
  }

  let { count, title, tone = 'accent', shape = 'pill' }: Props = $props();

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
  {#if shape === 'mark' || shape === 'inline-mark'}
    <span class="count-pill mark" class:inline={shape === 'inline-mark'} {title}
      ><Asterisk size="100%" strokeWidth={3.5} /></span
    >
  {:else}
    <span class="count-pill num" class:muted={tone === 'muted'} {title}>{shown}</span>
  {/if}
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
    font-weight: var(--weight-semibold);
    line-height: var(--leading-none);
  }
  /* Same pill, stated rather than announced: the badge fill drops to the
     surface every other quiet chip uses and the digits to muted text. */
  .count-pill.muted {
    background: var(--surface-badge);
    color: var(--text-muted);
  }
  /* The count itself is in the owning link's accessible name and title. */
  .count-pill.mark {
    position: absolute;
    left: calc(100% - 1px);
    top: 2px;
    min-width: 0;
    width: var(--unread-mark-size);
    height: var(--unread-mark-size);
    padding: 0;
    border-radius: 0;
    background: none;
    color: var(--accent-amber);
    pointer-events: none;
  }
  .count-pill.mark.inline {
    position: static;
  }
</style>
