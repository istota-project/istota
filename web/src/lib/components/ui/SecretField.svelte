<script lang="ts">
  import type { Snippet } from 'svelte';
  import Badge, { type BadgeVariant } from './Badge.svelte';
  import Button from './Button.svelte';
  import Field from './Field.svelte';
  import Input from './Input.svelte';

  interface Props {
    label: string;
    /** Optional guidance, rendered behind the "?" beside the label. */
    hint?: string;
    /** Passed to `Field`: the label in a column beside the control. */
    inline?: boolean;
    type?: 'text' | 'email' | 'password' | 'url';
    /** Whether a value is stored now. The value itself never reaches the page. */
    configured: boolean;
    /** What the user has typed in this session; never the stored value. */
    value: string;
    disabled?: boolean;
    /** A refusal about this value, rendered under the field. */
    error?: string;
    required?: boolean;
    onValueChange: (next: string) => void;
    /**
     * `immediate`: Clear sits beside the input and asks the caller, which
     * confirms and deletes at once (settings). `staged`: Clear sits after the
     * field and the clear becomes a pending change with an Undo, applied with
     * the rest of the form (the config editor).
     */
    clearMode?: 'immediate' | 'staged';
    /** Offered only while a value is stored. Without it there is no Clear. */
    onRequestClear?: () => void;
    /** `staged` only: the clear is pending, so the field says so and offers Undo. */
    pendingClear?: boolean;
    onUndoClear?: () => void;
    /** Where the value is held, as a badge after the label. */
    status?: { label: string; variant: BadgeVariant };
    /** Why the value cannot be changed here; shown in place of the input. */
    readOnlyReason?: string;
    /** Passed to `Field`: a small mark after the label. */
    labelMark?: Snippet;
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
    clearMode = 'immediate',
    onRequestClear,
    pendingClear = false,
    onUndoClear,
    status,
    readOnlyReason,
    labelMark,
  }: Props = $props();

  const layout = $derived(inline ? 'inline' : 'stacked');
  const showClear = $derived(configured && !!onRequestClear);
  const marked = $derived(!!status || !!labelMark);
</script>

{#snippet marks()}
  {#if status}<Badge size="sm" variant={status.variant}>{status.label}</Badge>{/if}
  {@render labelMark?.()}
{/snippet}

{#snippet clearButton()}
  <Button
    variant="secondary"
    size="sm"
    ariaLabel="Clear stored {label}"
    title="Clear stored value"
    {disabled}
    onclick={onRequestClear}>Clear</Button
  >
{/snippet}

<!--
  Set, replace or clear one credential. Never prefilled: the input holds only
  what was typed in this session, and a stored value is shown as a state
  (the placeholder, or `status`), never as text. The input is always the
  field's full 24rem, stored value or not, and Clear sits after it rather than
  inside that width: a button carved out of the input made every clearable
  field a step narrower than the plain ones beside it. Clearing stays a button
  rather than "empty the box", because an empty box already means "leave it
  as it is".
-->
{#if readOnlyReason}
  <Field {label} {hint} {layout} labelMark={marked ? marks : undefined}>
    <div class="secret-state">
      <span class="muted">{readOnlyReason}</span>
    </div>
  </Field>
{:else if clearMode === 'staged' && pendingClear}
  <Field {label} {hint} {layout} labelMark={marked ? marks : undefined}>
    <div class="secret-state">
      <span class="muted">Will be cleared.</span>
      <Button variant="secondary" size="sm" onclick={onUndoClear}>Undo</Button>
    </div>
  </Field>
{:else}
  <Field {label} {hint} {layout} {error} labelMark={marked ? marks : undefined}>
    <div class="secret-row hstack">
      <Input
        {type}
        {value}
        {disabled}
        {required}
        invalid={!!error}
        autocomplete="new-password"
        placeholder={configured ? '•••••••• stored, type to replace' : 'Enter value'}
        aria-label={(showClear && clearMode === 'immediate') || marked ? label : undefined}
        oninput={(e: Event) => onValueChange((e.currentTarget as HTMLInputElement).value)}
      />
      {#if showClear && clearMode === 'immediate'}{@render clearButton()}{/if}
    </div>
  </Field>
  <!-- Staged, Clear is drawn after the field, not inside it: the config
       editor caps a field at the label column plus 24rem, and a button inside
       that cap made every secret input a button narrower than the inputs
       beside it. -->
  {#if showClear && clearMode === 'staged'}
    <div class="secret-clear control-row">{@render clearButton()}</div>
  {/if}
{/if}

<style>
  /* The input's own 24rem cap as its basis, so the button follows it rather
     than taking width out of it. */
  .secret-row > :global(.ui-input) {
    flex: 0 1 24rem;
  }

  .secret-row > :global(.btn) {
    flex: none;
  }

  .secret-state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--control-height-lg);
  }

  /* On the input's line, as the editor's Reset is. */
  .secret-clear {
    display: flex;
    align-items: center;
    min-height: var(--control-height-lg);
  }
</style>
