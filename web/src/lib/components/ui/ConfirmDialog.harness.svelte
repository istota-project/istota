<script lang="ts">
  import ConfirmDialog from './ConfirmDialog.svelte';

  let {
    onConfirm = () => {},
    onCancel = () => {},
    onSecondary,
    secondaryLabel,
    challenge,
    confirmDisabled = false,
    error,
  }: {
    onConfirm?: () => unknown;
    onCancel?: () => void;
    onSecondary?: () => unknown;
    secondaryLabel?: string;
    challenge?: string;
    confirmDisabled?: boolean;
    error?: string;
  } = $props();

  let open = $state(true);
</script>

<!-- A bound parent, so a test can watch `open` change. -->
<ConfirmDialog
  bind:open
  title="Delete thing"
  message="Are you sure you want to delete the thing?"
  {onConfirm}
  {onCancel}
  {onSecondary}
  {secondaryLabel}
  {challenge}
  {confirmDisabled}
  {error}
/>
<output data-testid="open">{open}</output>
