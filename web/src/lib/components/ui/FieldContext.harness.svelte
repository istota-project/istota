<script lang="ts">
  import Field from './Field.svelte';
  import Input from './Input.svelte';
  import TextArea from './TextArea.svelte';
  import Select from './Select.svelte';
  import MultiSelect from './MultiSelect.svelte';
  import Checkbox from './Checkbox.svelte';

  let {
    control,
    description,
    error,
    required = false,
    optional = false,
    layout = 'stacked',
  }: {
    control: 'input' | 'textarea' | 'select' | 'multiselect' | 'checkbox' | 'raw' | 'buttons';
    description?: string;
    error?: string;
    required?: boolean;
    optional?: boolean;
    layout?: 'stacked' | 'inline' | 'compact';
  } = $props();

  let value = $state('a');
  let picks = $state<string[]>([]);
  let on = $state(false);
  let removed = $state(0);
  const options = [
    { value: 'a', label: 'Alpha' },
    { value: 'b', label: 'Beta' },
  ];
</script>

<!--
  One Field around each kind of control, as a form writes it, so the field
  context tests can ask what the caption does to each.
-->
<Field
  label="Caption"
  {description}
  {error}
  {required}
  {optional}
  {layout}
  checkbox={control === 'checkbox'}
>
  {#if control === 'input'}
    <Input bind:value />
  {:else if control === 'textarea'}
    <TextArea bind:value />
  {:else if control === 'select'}
    <Select bind:value {options} />
  {:else if control === 'multiselect'}
    <MultiSelect bind:value={picks} {options} />
  {:else if control === 'checkbox'}
    <Checkbox bind:checked={on} />
  {:else if control === 'raw'}
    <input />
  {:else}
    <button type="button" onclick={() => (removed += 1)}>Remove first</button>
    <button type="button">Remove second</button>
  {/if}
</Field>
<output data-testid="removed">{removed}</output>
<output data-testid="checked">{on}</output>
