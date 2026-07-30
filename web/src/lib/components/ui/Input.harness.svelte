<script lang="ts">
  import Input from './Input.svelte';
  import TextArea from './TextArea.svelte';

  let { multiline = false }: { multiline?: boolean } = $props();

  let value = $state('');
</script>

<!--
  A binding is a two-way contract, and Svelte 5 does not expose a $bindable
  prop on the component instance a test renders — so asserting it needs a real
  parent that binds. This is that parent: it echoes the bound value into an
  <output> the test can read.
-->
{#if multiline}
  <TextArea bind:value aria-label="Field" />
{:else}
  <Input bind:value aria-label="Field" />
{/if}
<output>{value}</output>
