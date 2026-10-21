<svelte:options runes={true} />

<script lang="ts">
  import Input from './Input.svelte';
  import TextArea from './TextArea.svelte';

  let {
    multiline = false,
    clearable = false,
    initial = '',
  }: { multiline?: boolean; clearable?: boolean; initial?: string } = $props();

  // svelte-ignore state_referenced_locally
  let value = $state(initial);
  let ref = $state<HTMLInputElement | undefined>();
  let areaRef = $state<HTMLTextAreaElement | undefined>();
</script>

<!--
  A binding is a two-way contract, and Svelte 5 does not expose a $bindable
  prop on the component instance a test renders — so asserting it needs a real
  parent that binds. This is that parent: it echoes the bound value into an
  <output> the test can read, and the bound element's tag into another.
-->
{#if multiline}
  <TextArea bind:value bind:ref={areaRef} aria-label="Field" />
{:else}
  <Input bind:value bind:ref {clearable} aria-label="Field" />
{/if}
<output data-testid="value">{value}</output>
<output data-testid="ref">{(multiline ? areaRef : ref)?.tagName ?? ''}</output>
