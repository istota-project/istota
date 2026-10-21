<svelte:options runes={true} />

<script lang="ts">
  import Field from './Field.svelte';
  import Input from './Input.svelte';
  import TextArea from './TextArea.svelte';
</script>

<!--
  Field around each shape of the two primitives, as a form writes them, so
  fieldCascade.test.ts can ask which of Field's rules reach which element.
-->
<Field label="Adorned">
  <Input aria-label="Adorned" prefix="$" />
</Field>
<Field label="Primitive">
  <Input aria-label="Primitive" monospace invalid />
</Field>
<Field label="Area">
  <TextArea aria-label="Area" />
</Field>
