<!--
  The shared `.spinner` (primitives.css), with its label for a screen reader.
  Without a label it is decoration, for a spinner beside text that already says
  what is running.
-->
<script lang="ts">
  interface Props {
    label?: string;
    /** `md` (the `.spinner` size, the default) or `sm` (`--icon-xs`). */
    size?: 'sm' | 'md';
  }

  let { label, size = 'md' }: Props = $props();
</script>

<span class="spinner" class:ui-spinner-sm={size === 'sm'} aria-hidden="true"></span>{#if label}<span
    class="visually-hidden">{label}</span
  >{/if}

<style>
  .ui-spinner-sm {
    width: var(--icon-xs);
    height: var(--icon-xs);
  }
</style>
