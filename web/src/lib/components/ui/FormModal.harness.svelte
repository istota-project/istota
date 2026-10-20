<script lang="ts">
  import Modal from './Modal.svelte';
  import FormModal from './FormModal.svelte';
  import Button from './Button.svelte';
  import Field from './Field.svelte';
  import Input from './Input.svelte';
  import TextArea from './TextArea.svelte';
  import Select from './Select.svelte';
  import Checkbox from './Checkbox.svelte';

  let {
    mode = 'form-modal',
    onSubmit = () => {},
    onCancel = () => {},
    onsubmit,
    busy = false,
    canSubmit = true,
    error,
  }: {
    mode?: 'form-modal' | 'modal';
    onSubmit?: () => void | boolean | Promise<void | boolean>;
    onCancel?: () => void;
    onsubmit?: (e: SubmitEvent) => void;
    busy?: boolean;
    canSubmit?: boolean;
    error?: string;
  } = $props();

  let open = $state(true);
  let name = $state('');
  let kind = $state('a');
  let notes = $state('');
  let on = $state(false);
  const options = [
    { value: 'a', label: 'Alpha' },
    { value: 'b', label: 'Beta' },
  ];
</script>

<!--
  A bound parent, so a test can watch `open` change, with one of each control
  the Enter rule has to hold for: a text input, a Select, a textarea and a
  checkbox.
-->
{#snippet fields()}
  <Field label="Name"><Input bind:value={name} /></Field>
  <Field label="Kind"><Select bind:value={kind} {options} fullWidth /></Field>
  <Field label="Notes"><TextArea bind:value={notes} /></Field>
  <Field label="Urgent" checkbox><Checkbox bind:checked={on} /></Field>
{/snippet}

{#if mode === 'modal'}
  <Modal bind:open title="Plain form" {onsubmit} {busy}>
    {@render fields()}
    {#snippet footer()}
      <Button variant="secondary" onclick={() => (open = false)}>Cancel</Button>
      <Button type="submit" variant="primary">Save</Button>
    {/snippet}
  </Modal>
{:else}
  <FormModal bind:open title="Edit thing" {onSubmit} {onCancel} {busy} {canSubmit} {error}>
    {@render fields()}
  </FormModal>
{/if}
<button type="button" onclick={() => (open = true)}>Reopen</button>
<output data-testid="open">{open}</output>
<output data-testid="name">{name}</output>
<output data-testid="kind">{kind}</output>
