<!--
  A link back to the page above this one. Always an `<a>`, never a button
  calling `goto()`, so it keeps middle-click and the status-bar URL.
-->
<script lang="ts">
  import { ArrowLeft } from '@lucide/svelte';

  let { href, label }: { href: string; label: string } = $props();
</script>

<a class="ui-back-link link-muted" {href}>
  <ArrowLeft size="1em" aria-hidden="true" />{label}
</a>

<style>
  .ui-back-link {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
  }
</style>
