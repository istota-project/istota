<script lang="ts">
  import Field from './Field.svelte';
  import Input from './Input.svelte';
  import TextArea from './TextArea.svelte';
</script>

<!--
  Field around one raw control and the two primitives, as a form writes them,
  so fieldCascade.test.ts can ask which of Field's rules reach which element.
-->
<Field label="Raw">
  <input aria-label="Raw" />
</Field>
<Field label="Primitive">
  <Input aria-label="Primitive" monospace invalid />
</Field>
<Field label="Area">
  <TextArea aria-label="Area" />
</Field>
