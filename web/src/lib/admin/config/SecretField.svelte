<script lang="ts">
  import { Badge, Button, Field } from '$lib/components/ui';
  import { SecretField } from '$lib/components/settings';
  import { credentialStatus, type CredentialStatus } from './credentialStatus';

  interface Props {
    label: string;
    /** Whether a value is stored now. The value itself never reaches the page. */
    configured: boolean;
    /** What the admin typed, `""` for a pending clear, undefined for unchanged. */
    pending: string | undefined;
    readOnlyReason?: string | null;
    /** Where the value is held: beside the label, or in place of the input
     *  when read-only. Set / Not set when absent. */
    status?: CredentialStatus;
    hint?: string;
    /** Passed to `Field`: the label in a column beside the control. */
    inline?: boolean;
    /** The new value, `""` to clear, or null to leave the credential as it is. */
    onChange: (value: string | null) => void;
  }

  let {
    label,
    configured,
    pending,
    readOnlyReason = null,
    status,
    hint,
    inline = false,
    onChange,
  }: Props = $props();

  let shownStatus = $derived(status ?? credentialStatus(configured));
</script>

{#snippet statusMark()}
  <Badge size="sm" variant={shownStatus.variant}>{shownStatus.label}</Badge>
{/snippet}

<!--
  Set, replace or clear one credential. Never prefilled: the input holds only
  what the admin typed in this session, and a stored value is shown as a state
  ("set" / "not set"), never as text. A clear is a pending change like any
  other, so it goes through the review dialog rather than happening on click.
-->
{#if readOnlyReason}
  <Field {label} {hint} layout={inline ? 'inline' : 'stacked'} labelMark={statusMark}>
    <div class="secret-state">
      <span class="muted">{readOnlyReason}</span>
    </div>
  </Field>
{:else if pending === ''}
  <Field {label} {hint} layout={inline ? 'inline' : 'stacked'} labelMark={statusMark}>
    <div class="secret-state">
      <span class="muted">Will be cleared.</span>
      <Button variant="secondary" size="sm" onclick={() => onChange(null)}>Undo</Button>
    </div>
  </Field>
{:else}
  <!-- Clear is drawn after the field, not inside it: the editor caps a field
       at the label column plus 24rem, and a button inside that cap made every
       secret input a button narrower than the inputs beside it. -->
  <SecretField
    {label}
    {hint}
    {inline}
    {configured}
    labelMark={statusMark}
    value={pending ?? ''}
    onValueChange={(next) => onChange(next === '' ? null : next)}
  />
  {#if configured}
    <div class="secret-clear control-row">
      <Button
        variant="secondary"
        size="sm"
        ariaLabel="Clear stored {label}"
        title="Clear stored value"
        onclick={() => onChange('')}>Clear</Button
      >
    </div>
  {/if}
{/if}

<style>
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
