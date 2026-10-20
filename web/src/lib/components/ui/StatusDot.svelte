<!--
  A live-status dot. It reads the saturated `--status-dot-*` family rather than
  the `-fg` tints: a dot carries no text, so it needs the chroma a tint cannot
  give it, and it keeps one value in both themes.
-->
<script lang="ts">
  interface Props {
    tone: 'success' | 'warn' | 'danger' | 'info' | 'neutral';
    /** `md` (0.5rem, the default) or `sm` (0.375rem). */
    size?: 'sm' | 'md';
    /** Names the dot for a screen reader; without it the dot is decoration
     *  and the text beside it carries the state. */
    label?: string;
    /** Placement only (a margin, an optical nudge). */
    class?: string;
  }

  let { tone, size = 'md', label, class: className = '' }: Props = $props();
</script>

<span
  class="ui-status-dot tone-{tone} size-{size} {className}"
  role={label ? 'img' : undefined}
  aria-label={label}
  aria-hidden={label ? undefined : 'true'}
></span>

<style>
  .ui-status-dot {
    display: inline-block;
    flex: none;
    border-radius: var(--radius-round);
    background: var(--text-dim);
  }
  .size-md {
    width: 0.5rem;
    height: 0.5rem;
  }
  .size-sm {
    width: 0.375rem;
    height: 0.375rem;
  }
  .tone-success {
    background: var(--status-dot-success);
  }
  .tone-warn {
    background: var(--status-dot-warn);
  }
  .tone-danger {
    background: var(--status-dot-danger);
  }
  .tone-info {
    background: var(--status-dot-info);
  }
  @media (forced-colors: active) {
    .ui-status-dot {
      background: CanvasText;
    }
  }
</style>
