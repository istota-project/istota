<script lang="ts">
  import Field from '../ui/Field.svelte';
  import Input from '../ui/Input.svelte';
  import Button from '../ui/Button.svelte';

  interface Props {
    label: string;
    /** Optional guidance, rendered behind the "?" beside the label. */
    hint?: string;
    /** Passed to `Field`: the label in a column beside the control. */
    inline?: boolean;
    type?: 'text' | 'email' | 'password' | 'url';
    configured: boolean;
    value: string;
    disabled?: boolean;
    /** A refusal about this value, rendered under the field. */
    error?: string;
    required?: boolean;
    onValueChange: (next: string) => void;
    onRequestClear?: () => void;
  }

  let {
    label,
    hint,
    inline = false,
    type = 'password',
    configured,
    value,
    disabled = false,
    error,
    required = false,
    onValueChange,
    onRequestClear,
  }: Props = $props();

  let showClear = $derived(configured && !!onRequestClear);
</script>

<!--
  Field + Input + a Clear button. The input is always the field's full 24rem,
  stored value or not, and Clear sits after it rather than inside that width:
  a button carved out of the input made every clearable field a step narrower
  than the plain ones beside it. Clearing stays a button rather than "empty
  the box", because an empty box already means "leave it as it is".

  `labelled` is false only while the clear button is present: a <button> is a
  labelable element, so inside a <label> it would become the label's implicit
  control and clicking the field's caption would clear the stored secret. With
  no button there is nothing to steal the association, and clicking the caption
  should focus the input as it does in every other field.
-->
<Field {label} {hint} {inline} {error} labelled={!showClear}>
  <div class="secret-row">
    <Input
      {type}
      {value}
      {disabled}
      {required}
      invalid={!!error}
      autocomplete="new-password"
      placeholder={configured ? '•••••••• stored, type to replace' : 'Enter value'}
      aria-label={showClear ? label : undefined}
      oninput={(e: Event) => onValueChange((e.currentTarget as HTMLInputElement).value)}
    />
    {#if showClear}
      <Button
        variant="secondary"
        size="sm"
        ariaLabel="Clear stored {label}"
        title="Clear stored value"
        {disabled}
        onclick={onRequestClear}>Clear</Button
      >
    {/if}
  </div>
</Field>

<style>
  .secret-row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }

  /* The input's own 24rem cap as its basis, so the button follows it rather
     than taking width out of it. */
  .secret-row > :global(input) {
    flex: 0 1 24rem;
  }

  .secret-row > :global(.btn) {
    flex: none;
  }
</style>
