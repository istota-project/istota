<script lang="ts">
  import { SecretField } from '@minot/ui';
  import { credentialStatus, type CredentialStatus } from './credentialStatus';

  interface Props {
    label: string;
    /** Whether a value is stored now. The value itself never reaches the page. */
    configured: boolean;
    /** What the admin typed, `""` for a pending clear, undefined for unchanged. */
    pending: string | undefined;
    readOnlyReason?: string | null;
    /** Where the value is held: beside the label. Set / Not set when absent. */
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
</script>

<!--
  The config editor's policy over the shared SecretField: a stored value is
  shown by where it is held, a managed or overridden credential by why it
  cannot change, and a clear is a pending change like any other, so it goes
  through the review dialog rather than happening on click. `""` in the draft
  is that pending clear.
-->
<SecretField
  {label}
  {hint}
  {inline}
  {configured}
  status={status ?? credentialStatus(configured)}
  readOnlyReason={readOnlyReason ?? undefined}
  clearMode="staged"
  pendingClear={pending === ''}
  value={pending ?? ''}
  onValueChange={(next) => onChange(next === '' ? null : next)}
  onRequestClear={() => onChange('')}
  onUndoClear={() => onChange(null)}
/>
