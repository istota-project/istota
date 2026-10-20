<!--
  A pane's loading state. It holds the pane's slot with an empty `.center-msg`
  and raises the one fixed loader in the root layout (`PageLoader`) while it is
  mounted. The visible indicator never sits in the pane: a centred "Loading…"
  moved every time the nav and then a module sidebar rendered around it
  (ISSUE-752). The label is plain hidden text, read in place: no live region, since one
  mounted with its text already in it is not announced.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { holdPageLoading } from '$lib/ui/pageLoading';

  let { label = 'Loading…' }: { label?: string } = $props();

  onMount(() => holdPageLoading());
</script>

<div class="center-msg page-loading">
  <span class="visually-hidden">{label}</span>
</div>
