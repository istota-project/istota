<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Disclosure } from '@minot/ui';

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
  away under one line.
-->
<Disclosure summary={label} {open}>
  <div class="setup-more-body">
    {@render children()}
  </div>
</Disclosure>

<style>
  .setup-more-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
</style>
