<script lang="ts">
  import { Badge, Button, Field } from '$lib/components/ui';
  import { SecretField } from '$lib/components/settings';

  interface Props {
    label: string;
    /** Whether a value is stored now. The value itself never reaches the page. */
    configured: boolean;
    /** What the admin typed, `""` for a pending clear, undefined for unchanged. */
    pending: string | undefined;
    readOnlyReason?: string | null;
    hint?: string;
    /** The new value, `""` to clear, or null to leave the credential as it is. */
    onChange: (value: string | null) => void;
  }

  let { label, configured, pending, readOnlyReason = null, hint, onChange }: Props = $props();
</script>

<!--
  Set, replace or clear one credential. Never prefilled: the input holds only
  what the admin typed in this session, and a stored value is shown as a state
  ("set" / "not set"), never as text. A clear is a pending change like any
  other, so it goes through the review dialog rather than happening on click.
-->
{#if readOnlyReason}
  <Field {label} {hint} labelled={false}>
    <div class="secret-state">
      <Badge variant={configured ? 'success' : 'partial'}>{configured ? 'set' : 'not set'}</Badge>
    </div>
  </Field>
{:else if pending === ''}
  <Field {label} {hint} labelled={false}>
    <div class="secret-state">
      <span class="muted">Will be cleared.</span>
      <Button variant="ghost" size="sm" onclick={() => onChange(null)}>Undo</Button>
    </div>
  </Field>
{:else}
  <SecretField
    {label}
    {hint}
    {configured}
    value={pending ?? ''}
    onValueChange={(next) => onChange(next === '' ? null : next)}
    onRequestClear={() => onChange('')}
  />
{/if}

<style>
  .secret-state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
</style>
