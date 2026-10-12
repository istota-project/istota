<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    /** The disclosure's caption. */
    label?: string;
    /** Open it, for an error under a field inside that would otherwise be hidden. */
    open?: boolean;
    children: Snippet;
  }

  let { label = 'More', open = false, children }: Props = $props();
</script>

<!--
  The setup screens' "More": the settings most people leave alone, folded
  away under one link-styled line.
-->
<details class="setup-more" {open}>
  <summary>{label}</summary>
  <div class="setup-more-body">
    {@render children()}
  </div>
</details>

<style>
  .setup-more summary {
    width: fit-content;
    color: var(--link);
    cursor: pointer;
  }

  .setup-more-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-top: var(--space-2);
  }
</style>
